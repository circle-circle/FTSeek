# DEB-20261105 · 120s + 20% holdout + mid/grace 机制加固

> 状态：观测与结算修复已实施；**现役交易判定不变，盈利能力仍 UNPROVEN**。  
> 起因：用户质疑“确认是真的吗”，并要求聚焦现役组合的合理优化。  
> 边界：不改 120 秒、20% 分流、mid/grace 判定、追价阈值、仓位或出场。

## 1. 先纠正确认状态

- 当前 RFC-158 活跃 cohort 仍为 **n=0**，不存在“本轮已验证有效”的事实。
- 旧 v2 归档 112 个 treatment 决策中，真正 `ruler_used=mid` 仅 **61**，另有 **51 个 legacy 降级**；不能把整段表现归因于 mid。
- 归档 52 条 grace：46 条后续两逆放弃、6 条恢复确认；没有第三次 follow-up 决定结果，也没有 `unpersisted`。
- 因而现役实际语义是 **“120s 首逆 + 后续持续性复核”**，不是严格三点 2-of-3。配置注释和 RFC 已补充这一事实。

## 2. 发现并修复的 M4 双向计量错误

### 锚点错误

旧 `_avoided_bps` 使用 post 行的 `mid_move_pct`，其基准是 `mid_arm`。这把 `arm→弃单` 已发生的变动也算进“弃单避免额”，不是弃单决策的边际价值。

### SHORT 符号错误

`mid_move_pct` 已经由 `eval_confirm` 做方向归一（正=顺原交易方向），旧结算却对 SHORT 在 `mv>0` 时记避免额，方向与 LONG 不一致。

归档 v2 可配对 30 笔的同尺复核：

| 口径 | 避免额合计 |
|---|---:|
| 旧结算实际值（arm 锚 + SHORT 二次方向判断） | 1251.72bps |
| 仅修方向、仍用 arm 锚 | 2046.27bps |
| 同时修方向与锚点（弃单 mid→post mid） | **1017.11bps** |

两个错误会相互抵消，不能简单说旧值统一高估或低估；分方向和经济价值均不可沿用。

### 新契约

- post 行新增 `mid_from_confirm_pct` / `last_from_confirm_pct`：弃单时点→+5m/+10m，单位百分比，正=顺原交易方向。
- M4 优先使用 mid→mid；mid 不可用时显式使用 last→last，并记录 basis 计数。
- LONG/SHORT 统一 `avoided_bps=max(0,-move_pct*100)`。
- 缺新锚字段的旧行不进入新 M4；弃单后验覆盖不完整时 M4 不得判 PASS。

## 3. R1 真正 2-of-3 路径影子

现役判定完全不变，新增 `path_shadow`：

- 60/90/120 秒记录 mid、last、bid/ask、quote_ts、book_status、方向归一 move 与实际采样秒数。
- 三个点均为有效 mid 且采样误差 ≤20 秒时才可评估；缺点、legacy 或超时只标原因，不伪造票数。
- `r1_would_confirm = (三个有效 mid 同侧票 ≥2)`；同时记录它与最终 live/grace 是否一致。
- grace 首逆时的 120 秒样本被冻结，后续 130/140 秒报价不会覆盖它。
- 重启后缺哪些点就标哪些点；若重启发生在 60 秒前且后续三点完整，不人为丢弃。
- 观测异常被隔离，不得改变 CONFIRMED/ABANDONED、下单或 SKIP。

这不是上线 R1，而是首次获得能判断“确认次数是否改善”的同笔路径数据。

## 4. 20% holdout 与 mid 降级

- holdout 仍用 SHA1(`entry_confirm_holdout:{inst_id}`)，跨轮/重启稳定，**分配不变**。
- 在归档 166 个决策上重放分配：总体 33/166=19.9%；SHORT 15.8%、LONG 22.0%，开盘 18.1%，later 小样本 31.8%。总体无失效，但子群可能失衡，故结算新增 direction/session/archetype 分层，而非现在改算法。
- v2 报价时间戳未见 >15s 的正 stale 样本，暂不增加未经验证的 stale 硬门。
- `book_status` 与 R1 unevaluable reason 将量化 proxy/missing/crossed 和 mid 覆盖率；未来是否把“mid 不可用”从 legacy 降级改为等待，必须另做影子经济比较。

## 5. 后续裁决门

1. 现役 RFC-158 按 §7 的首样本前用户目标澄清积累；R1 影子规则不变。
2. R1 至少需要 **30 个 evaluable 样本且覆盖 10 日**，再比较同笔净经济价值、误杀赢家、机会成本与日聚类 MDE；不得只比较通过率。
3. R1 与 live 不一致的样本必须双向列出：`live CONFIRMED/R1 ABANDON` 可用实际 SIM PnL，`live ABANDON/R1 CONFIRM` 用弃单后路径，不能只看一侧。
4. holdout 分层只作失衡披露，除非另立预注册，不自动改分流或阈值。

## 6. 验证与回滚

- 相关四套件：**114 passed**；entry_confirm/live wiring 单独复跑 **86 passed**。
- 当前结算器空 cohort 正确输出 `TOO_EARLY`、R1 `evaluable_n=0` 与 holdout 空分层。
- 无 YAML 数值变化；仅注释澄清。回滚为 revert 本批代码/报告字段，不涉及交易参数恢复。
- 08:22 按仓规重启，启动冒烟 **7/7**；服务 active，启动段无 Traceback/CRITICAL/ERROR。
- 重启前后 US.COIN、US.ALB 两笔未平仓保持不变；配置实读仍为 `120s/20%/mid/grace=3+2`，FADE cap=false。

## 7. 首样本前用户目标澄清：量大后重开，减大亏优先

- 用户说明 120 秒门是其原始设计；此前取消的一项原因是指令量与成交量下降。当前 DA 每日指令量已提高，
  重开目的在于降低大亏损，接受更少的小盈利和更低的盈利胜率。
- 活跃 cohort 仍为 0 行，故在读数前将 RFC-158 §3 的 M1~M4/E1~E2 硬门改为诊断项；
  现行唯一验收见 `RFC-158-entry-confirm-reopen-v2-preregistration.md` §10。
- 新主轴为同期 treatment/holdout 的 resolved assignment：treatment 弃单实际净额=0，
  确认/holdout 必须按 decision_id 联接已平 PLAYBOOK_SIM，未成熟行不得补 0。
- PASS 要求每分配指令 SL_INITIAL 亏损负担下降、日聚类 80%CI 下界>0，且总净额/分配不恶化。
  胜率、机会成本、小赢家数量和交易数量不再自动触发 FAIL。
- 样本门为 treatment resolved≥60、holdout resolved≥20、同期配对≥10 ET 日、两臂覆盖率≥95%。
  FAIL 只提请复核，不自动关门；INSUFFICIENT/OBSERVE 均不等于已证明减亏。
- 结算器新增 `current_cohort.tail_objective`；注册表计数锚改为 treatment resolved n。
  现役 120s、20% holdout、mid/grace 和全部下单/退出参数均未改变。
