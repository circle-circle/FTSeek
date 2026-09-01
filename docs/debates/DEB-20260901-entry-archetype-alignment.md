# DEB-20260901: 指令-触发语义对齐 — entry_archetype 结构化 schema + 确认位影子执行（提请裁决）

- **立案日**: 2026-09-01（8/31 盘后全链复盘 → SL_INITIAL/mfe=0 溯源的下游提案；用户令"按照建议执行"）
- **裁决**: 2026-09-01 用户批准**选项 1**（Phase A+B + P1 影子）。
- **状态**: **Phase A/B + P1 已落地（影子态，零交易行为变更）**；等影子攒样到预注册判据（§五）再裁 Phase C。
- **纪律引用**: 影子先行（C4）；退出参数冻结至 9/30（C1，本档不触碰）；预注册单假设（KR3）；一次一个变量；不新造并行熔断层；打标前不切实盘（C2）。
- **关联**: DEB-20260831（SL_INITIAL/mfe=0 溯源，本档上游）、DEB-20260806（锚限价影子）、DEB-20260830（UE 模板字段实验）、RFC-071 `44d6c841`（死因诚实化，同日）。

---

## 〇、问题定义：DA 写的是"低吸单"，执行层跑的是"突破单"

### 0.1 三个实锤案例（8/31 盘后复盘取证）

**案例 A — US.NOW（8/31，昨晚）**
- bridge 原文（`data/opportunity_scan_2026-08-31.json`）：entry_strategy = "分2批低吸：第1批 **$140.50 挂限价单**，触发条件为盘前成交量>5万股且价格企稳5分钟；第2批 $138.40…"；thesis 明示"锚定盘前$142.18**分批低吸**"。
- 实际执行链：`re-price: 盘前=$139.45 → 实时=$145.43`（重锚定）→ `方向穿越触发`（09:36）→ **市价追单成交 $145.09**（比指令确认位高 **+3.26%**）。
- 结果：盘中最高 $149.6（TP=149.5 几乎触达），但出场为 SL_TRAIL_LOCK @$145.87，净 +$26.85，归因"出场偏早、严重回吐(38%)"——入场点过高导致全程贴地，锁盈机制只能锁到保本附近。
- **反事实（诚实披露）**：当日 1m bar 最低 $141.69，**从未触及 $140.50** → 若按 PULLBACK_LIMIT 执行，此单当天不会成交（放弃 +$26.85，也免除全部风险）。确认位入场的本质是"只在条件满足时进场"，入场数下降是设计目标，机会成本须用 fwd shadow 计量（见 §五）。

**案例 B — US.AVGO（8/31）**
- bridge 原文：白纸黑字"**禁止在 $370 以上追涨**"，首批区间 $362.50–363.50。
- 实际：re-price 把入场位顶到 $368.45 → $369.62（贴红线），09:36–15:41 ET 反复"方向穿越触发"。（最终被重复开仓拦截——存量 SHORT 16 股方向冲突，属另一问题，见 §五.3。）
- 语义倒挂：DA 要求"回调至支撑区才买"，触发器却因重锚定在价格高位反复尝试买入。

**案例 C — US.AFRM（8/28，DEB-20260831 已立案）**
- thesis 自写"**放弃开盘抢筹，等回踩 $82.5–83.5**"，第1批限价区 $83.5–84。
- 实际：09:45 市价成交 $86.37（超限价区 +3%）→ 9 分钟后 SL_INITIAL，净 -$308。
- 执行层"合规"的原因：落库区间 $80.99–87.68，开盘价恒在区间内——**区间无语义，合规即失效**。

### 0.2 根因链（四层叠加）

1. **执行层只消费 `entry_price + entry_low/entry_high 宽区间 + SL`**（DEB-20260831 §1.1 grep 实证）；确认位/量价条件/"禁止追涨"是纯装饰文本。
2. **re-price 重锚定**（`entry_reanchor`）：盘前锚定开盘即被改为实时价，"等回踩"语义退化为"现价追"。
3. **方向穿越触发**（`app/playbook/pipeline.py:4987`）：BUY = 价格回落到入场价买入（有利侧）/ 越过入场价追入需量能确认——设计本身区分了"回调买入"与"追入"，但第 2 层重锚定让每笔单都变成"刚锚定就穿越"的追入形态。
4. **分批不存在**：执行路径无任何第二腿逻辑（DEB-20260831 实证），thesis 的"首批试探/二批确认"100% 一次性市价。

### 0.3 统计佐证（本会话独立复核，6/27–8/31，PLAYBOOK_SIM，n=390 已平仓）

**出场结构——利润来自单一引擎，出血来自三类死单：**

| 类别 | n | 胜率 | 净 |
|---|---|---|---|
| SL_TRAIL_LOCK（利润引擎） | 88 | 94.3% | **+3,783** |
| SL_INITIAL | 14 | 0% | -2,621 |
| MFE_STALL_EXIT | 31 | 0% | -1,353 |
| TIME_EXIT | 36 | 0% | -1,195 |
| OVERNIGHT_GAP_STOP / DEAD_SIGNAL / 其余 | 31 | — | -869 |

**胜率不是瓶颈，左尾才是**：`conf=0.7 LONG` n=103、胜率 **60.2%**，但净 **-286**（平均赢 +49.2 vs 平均亏 -81.4，最差单笔 -386.8）。

**跳空对齐度 × 开盘窗口分叉（LONG，按 `metadata.entry_gap.aligned_gap_pct`）：**

| 单元 | n | 胜率 | 净 |
|---|---|---|---|
| 09:30–10:00 × 高开 0–4% | 85 | **62%** | **+594** |
| 09:30–10:00 × 平开/低开 | 100 | 52% | **-1,092** |
| 09:30–10:00 × 高开 4%+ | 7 | 71% | -479（赢小亏大） |
| 10:00 后全部 | 97 | 27–43% | -190 |

各格胜率只差 ~10pp，期望值差 4 位数——**系统的真实边际 = 开盘窗口、温和高开多头动量**；平开/低开接多（"回踩非反转"句式猎场，DEB-20260831 §1.2 五连亏 -$1,117）与追 4%+ 大缺口是两个稳定血池。方向穿越触发 + 无条件重锚定恰好系统性地把单子送进血池。

## 一、提案 A（P0）：bridge schema 结构化字段 + archetype 路由

### 1.1 新增字段草案（加在 `candidates[]`，DA skill 侧产出）

```json
{
  "entry_archetype": "BREAKOUT | PULLBACK_LIMIT | FADE_SHORT",
  "confirmation": {
    "price_level": 140.50,
    "side": "below",
    "ttl_min": 15,
    "vwap_side": "above | below | null",
    "min_volume": 50000,
    "settle_minutes": 5
  },
  "chase_cap_price": 118.00
}
```

- `entry_archetype`：BREAKOUT=突破追入（现状行为）；PULLBACK_LIMIT=等回踩确认位限价接；FADE_SHORT=等反弹至确认位做空（EIX 类场景）。
- `confirmation.side`：相对确认位的等待方向（BUY 等 `below` / SHORT 等 `above`）。
- `ttl_min`：限价挂单存活窗口，超时撤单 = 当日放弃该指令（记 `EXPIRED_UNFILLED`）。
- `chase_cap_price`：禁追上限（DA 已在自由文本里写"禁止在 $X 以上追涨"，结构化后成为硬约束：`entry_high := min(entry_high, chase_cap)`，re-price 不得越过）。
- **兼容默认**：字段缺失 → `BREAKOUT` → 与今日行为完全一致（零行为变更，直到 DA 开始产出新字段）。

### 1.2 为什么相信 DA 填得出这些字段

DA 昨晚 7 个候选中 **每一个**都已在 `entry_strategy` 自由文本里写明了确认位、量价条件、禁追价（见 §0.1）——信息已存在，只是非结构化。DEB-20260830 E3 实验已验证路径：字段进模板后填充率 0%→83–100%（8 臂实弹）。判据沿用该立案：10 交易日填充率 ≥50% 白名单，否则 revert。

### 1.3 执行层路由（批准后实施，影子先行）

| archetype | 触发/执行 | re-price 行为 |
|---|---|---|
| BREAKOUT | 现状不变（方向穿越 + 追入量能确认） | 现状（重锚定） |
| PULLBACK_LIMIT | 确认位限价挂单 + TTL 撤单；未成交 = 不进场 | **不向确认位上方重锚定**；越过 chase_cap → 指令作废（skip_reason=`CHASE_CAP_VIOLATION` 白名单）+ 遥测 |
| FADE_SHORT | 同 PULLBACK_LIMIT（方向镜像） | 同上 |

限价单基础设施复用出场侧已验证路径（`DualBroker` 限价→超时降级，见 8/31 NOW 平仓实跑）。

## 二、提案 B（P1）：gap_alignment_shadow（纯影子，不拦截）

- 每笔 playbook 入场落一行：`aligned_gap_pct × archetype × 窗口 × fwd_return_intraday/t1`。
- 与在研 `-2%~-5%` 真空带 A/C 影子门**并池**（同属"弱开盘多头"假设），不新造门、不新造并行层。
- 观察目标即 §0.3 表格的 go-forward 验证：平开/低开 LONG 与高开 4%+ 是否持续负期望。

## 三、既有证据盘点（全部已存在，本档未新造数据）

| 证据 | 来源 | 结果 |
|---|---|---|
| 确认位反事实重放 | DEB-20260831 §二（本会话复跑一致） | 5 对 -1,412→-796，**减亏 44%**，量价未重放=下界，无一转正 |
| 锚限价影子 5 分钟 TTL | DEB-20260806（n=179 触发，配对 108） | fill_rate 1.0，**Δ合计 +416.8（均 +3.86/笔）** |
| 锚限价影子 30 分钟 TTL | 同上 | fill_rate 0.75，Δ合计 -224.4（漏单主导）→ **短 TTL 优于长 TTL** |
| 结构化字段填充率实验 | DEB-20260830 E3 | 字段进模板 → 填充率 83–100% |
| 旗标→回吐幅度（非方向） | DEB-20260831 §三 | 旗标单胜率 46.2%≈无旗标 46.1%，单均 -8.1 vs +4.3 |

## 四、实施路径（四阶段，影子先行，一次一个变量）

- **Phase A（批准后）**：DA skill 模板加字段，双轨输出（自由文本照旧 + 结构化字段新增）；FTSeek 侧只解析落库（`playbook_instructions.metadata_json`），零行为变更。
- **Phase B**：执行层影子——PULLBACK_LIMIT 指令按现状执行（不变！），同时记录 would-fill / would-expire / 反事实 PnL，扩展 `cf_confirmation_replay.py` 样本；chase_cap 违例遥测。
- **Phase C**：预注册判据（§五）达标 → PULLBACK_LIMIT 切实盘（**仅 sim**）。
- **Phase D**：gap_alignment 证据池到量 → 独立裁决。

## 五、预注册判据（判据字典口径）

- **P0（Phase C go/no-go，单假设："确认位限价入场优于市价穿越入场"）**：n≥20 可配对 PULLBACK_LIMIT 样本，三条全过才切：① 净期望改善 ≥30%；② 成交率 ≥50%（过低=策略性空转，机会成本不可接受）；③ mfe=0 占比下降。任一条显著劣化 → 撤回，证据留档。
- **P1（gap_alignment）**：各格 n≥30 后，用 `gate_cost_report.py` 方法计算拦截成本（被拦单 `fwd_return_intraday` 均值）；仅当拦截成本 < 保留收益才进入门禁立项。
- **DA 填充率**：沿用 DEB-20260830 判据③（10 交易日 ≥50% 白名单通过，否则 revert 模板）。

## 六、诚实披露与风险

1. **入场数会下降**：NOW 案例证明确认位可能全天不触及——系统从"总是追进去"变为"满足条件才进"。这是设计目标，但机会成本必须用 Phase B 的 fwd shadow 量化（判据②即为此设）。
2. **量价条件未重放**：§三所有确认位证据均为价格条件下界。
3. **AVGO 方向冲突**：8/31 DA 提名 BUY 而存量 SHORT 16 股，重复开仓拦截兜住了仓位，但 DA 不感知存量持仓方向是独立缺口，建议列入下一议题（本档不展开）。
4. **数据卫生**：`limit_entry_shadow_replay_latest.json` `reconciliation_ok=false`（shadow 行与 entry_timing 行数不匹配，8/7–8/24 逐日差 1–3 行），Phase B 复用该管线前需先修复或注明口径。
5. **样本时代性**：§0.3 统计含 8/6 前旧触发逻辑样本（±0.5% zone 时代），分叉结论以方向穿越时代子样本复核为准（Phase B 影子天然只采新样本）。

## 七、明确不做（已证伪/越界）

- 降 confidence（DEB-20260831 §三 证伪：旗标不预测方向）。
- 同向齐射上限（DEB-20260831 §五 证伪撤回）。
- 冻结期内动任何退出参数（C1，至 9/30）。
- 新造熔断/并行门层（查 `docs/circuit-breaker-map.md` 纪律）。
- 切实盘（C2，四锁+毕业门禁+用户签字）。

## 八、变更清单（批准后执行，全部可回滚）

| 件 | 路径 | 回滚 |
|---|---|---|
| DA 模板字段 | 仓外 `/root/.hermes/skills/deep-stock-analysis/`（先备份） | 恢复备份文件 |
| FTSeek 解析落库（Phase A，只读行为） | `app/playbook/pipeline.py` bridge 解析段 | git revert |
| 执行层影子（Phase B） | `app/playbook/pipeline.py` + `data/shadow/pullback_limit_shadow.jsonl` | 配置开关置 false（新键入 census EXTERNAL_CONSUMED 登记） |
| PULLBACK_LIMIT 限价路径（Phase C，暂不实施） | `DualBroker` 入场侧 | 同上 |
| gap_alignment_shadow（P1） | 独立影子脚本 + jsonl | 删文件 |

## 九、裁决点

- **选项 1（建议）**：批准 Phase A + B（DA 双轨 + 执行层影子，零行为变更）+ P1 影子同期注册。
- **选项 2**：仅批准 P1（gap_alignment 影子），P0 继续攒样。
- **选项 3**：维持现状，本档留档待 9/30 冻结期结束后复议。

## 十、落地记录（2026-09-01，用户裁选项 1 后执行）

| 件 | 位置 | 说明/回滚 |
|---|---|---|
| DA 模板字段 | `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` execution_plan schema + 判定规则 | 备份 `.bak-deb20260901`；回滚=恢复备份 |
| DA 透传 | 同上 `adapter.py::to_voting_result`（+3 字段） | 备份同上 |
| DA 确定性兜底 | 同上 `pipeline.py::_derive_entry_semantics`（LLM 缺失时从 entry_strategy/thesis 文本抽取；保守：不明确→None）+ cand/raw_instruction 透传 | 备份同上 |
| FTSeek 透传链 | `app/playbook/instruction_generator.py`（Instruction +3 字段）、`app/playbook/pipeline.py`（bridge→vote→Instruction→raw_instruction 落库） | git revert |
| Phase B 影子 | `app/playbook/pipeline.py` 触发点落 `data/shadow/pullback_limit_shadow.jsonl`（type=trigger/chase_cap_violation）；开关 `playbook.pullback_limit_shadow_enabled`（默认 true，playbook 顶层，反向普查已登记） | 开关置 false + 重启 |
| 次日重放 | `scripts/cf_pullback_limit_replay.py`，crontab `47 5 * * 2-6` → `logs/cf_pullback_limit_replay.log` | 删 crontab 行 + 删脚本 |
| P1 影子 | `scripts/gap_alignment_shadow.py`，crontab `52 5 * * 2-6`；历史已回填 6/27–8/31 共 390 笔（与 §0.3 表逐格一致） | 删 crontab 行 + 删脚本 + 删 jsonl |
| §六.4 数据卫生修复 | ① `pipeline.py::_shadow_dedup_ok` 文件级防重（锚限价影子重启重复根因修复）② `scripts/limit_entry_shadow_replay.py` 对账哨兵两侧同口径按 (inst_id,日) 去重 → `reconciliation_ok=true` | git revert |

**验证实录**: 确定性兜底对 8/31 全部 7 候选解析正确（NOW=140.5/AVGO cap=370/BABA cap=118/EIX=FADE_SHORT 63.27/BWXT 试多句式识别/AFRM 回归=83.5）；`_shadow_dedup_ok` 跨重启防重单测通过；全量测试通过（基线一致）；重放脚本 8/31 空跑正常（当日指令无结构化字段，n=0 合预期）。

**今晚（9/1 盘）起自动生效**: DA 盘前 bridge 将携带结构化字段（双轨：自由文本照旧）；FTSeek 侧仅记录不改变任何执行动作。首批影子判读在 9/2 05:47 CST 重放。

## 十一、Phase C 三观察点总纲（2026-09-01，用户裁"要"立项）

> 缘起：8/31 全链复查（分源：DA +466/14天健康，UE -122 出血点，重武装 83% 胜率最优）后，
> 用户裁"要"立三个观察点，并严令：**不要零散散落、避免遗忘丢失、充分检查设计、不重复造轮子**。
> 防散落三登记：全部注册 `app/playbook/shadow_registry.py`（单一真源）+ 判据字典（B14 + B1 补注）
> + 回放台账；周检 `check_shadow_registry()` 自动可见，不存在游离观察。

### ① 缺口反向门 → 归并 B1，不新建（盘点结论：轮子已存在）

- `gap_chase_gate`（B1，8/29 注册）的 aligned_gap≥5% 口径已含顺向追 + 逆势追两族，无需新门。
- 勘误：8/31 PCG 实为 **-19.8% 崩跌追空**（prev_close 16.6→open 13.31），非"缺口上沿做空"，
  正落入 B1 拦截队列。
- 缺口发现：B1 注册后 **eval 脚本无 cron 挂载（悬空观察）** → 本轮补挂 `10 6 * * 2-6`。

### ② DA/UE/重武装分源切片 → `gap_alignment` 扩展（原料已存在，只加切片）

- `scripts/gap_alignment_shadow.py` 扩展：+`source`（DA/UE/REENTRY/OTHER，按 decision_id 前缀）
  +`mfe_pct` 字段；summary +`by_source` 切片（mfe0_rate 分母仅计带 mfe_pct 的行）；
  +trade_id 幂等去重（重跑不重复累加）；已追溯回填 390 存量行。
- 基线（6/27-8/31）：DA n=271 净 -770 mfe0_rate=9.2% / OTHER n=50 / REENTRY n=13（UE 笔数少，逐日累计）。
- 消费：9/2 首批判读报告六节之一 + 周检 by_source 可见。注册表 `gap_alignment`（count_mode=distinct:trade_id）。

### ③ DA 被拦信号前瞻 → 新脚本 `da_blocked_signal_cf_eval.py`（唯一新建，判据 B14）

- 回答："拦太狠"自警 + BABA/BWXT $3000 地板误杀案需要系统性证据 — 被 DA 门拦掉的方向性候选
  （direction∈BUY/SELL_SHORT，SKIP 行排除）若放行，前瞻是赚是亏。
- 数据源全部已存在：`data/da_gate_decisions.jsonl`（7/13 起）+ `data/replay_bars_daily/` 日K
  （AFTER_HOURS 增量刷新）；锚=锚日开盘价；方向带符号收益；删失显式计数（口径对齐
  `gate_counterfactual_ruler.py`，不新造约定）。
- 基线首跑（9/1）：n=139（去重）可评估 126；T+1 mean=-0.088% WR=46.0% 费用线内不显著；
  T+3 mean=-0.155%（p_under=0.043）/ T+5 mean=-0.778%（p_under=0.022）→ 方向上"拦得对"，
  无过度拦截证据；预注册裁决=KEEP_SAMPLING（主窗 T+1 未越线不显著）。
- 挂载：`15 6 * * 2-6`；注册表 `da_blocked_cf`（min_n=50，计数锚=latest 顶层 n）。

### 裁决日与联动

- 9/2 05:47 首批判读（一次性报告 `deb20260901_shadow_first_report.py`）覆盖 ①② 进度。
- ③ 每日机械评估，B14 判据自动出裁决；两连 OVER_BLOCKING_SUSPECT 才提案另立 DEB。
- 与既有窗口关系：不触碰 9 月退出参数冻结（C1/C7）；不翻 `entry_archetype_routing_enabled`
  （判据三条全过 + 用户裁决前）；与 B13（再入场快TP）观察窗并行互不干扰。
