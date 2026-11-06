# AUDIT-20261106 全链路复盘：信息输入 → 推理 → 入场 → 出场

> 触发：2026-11-05 崩盘日 DA 仍全力做多（净 −$115.92）。复核后确认**那是症状不是病**。
> 口径：A10 era-clean（`app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE`，2026-07-01 起，
> 剔 `POSITION_SYNC`/`MANUAL`，`exit_price > 0`）⇒ **n=765**。
> 复现：`venv/bin/python3.11 scripts/audit_full_chain_20261106.py`（只读 SELECT，一条命令出全部数字）。
> 性质：**只读审计**。不改 config、不加 cron、不写生产库、不下单。改进项一律走 DEB 预注册。

---

## §0 结论摘要

1. **毛利是正的，费用把它吃穿**：毛额 **+$2,279.51**、费用 **$4,240.44**（毛额的 **186%**）、净 **−$1,961.00**。
2. **胜率不低但赔率不够**：胜率 63.1%，avg win +$30.54 / avg loss −$59.27，**payoff 0.52** ⇒ 不含费也要 65.8% 胜率才打平，含费更高。
3. **亏损高度集中在两个尾部**：`SL_INITIAL` 41 笔 **−$5,981**（均 −$146）+ `OVERNIGHT_GAP_STOP` 14 笔 **−$4,531**（均 −$324）= −$10,512；正贡献几乎只有 `SL_TRAIL_LOCK` 196 笔 +$8,754 与 `PRE_CLOSE_EXIT` +$1,598。
4. **reasoning→action 的真实缺口不是"缺一个风控门"，是"DA 没有组合级前提可表达"**：它的输入是 19 维个股 + 一个 regime 标签，被迫逐票答方向；且同输入 12 次复跑方向 11:1、score 6↔8 ⇒ 单次结论不可归因。
5. **入场质量是最大可动杠杆**（不是阈值）：MFE 中位仅 **0.60%**、**19.5% 的交易 MFE < 0.2%**（入场即错），而名义额中位 $5,618 低于佣金地板决定的盈亏平衡 **≈$8,964**。

---

## §1 链路七段与断点

| 段 | 载体 | 实际行为 | 断点 |
|---|---|---|---|
| ① 采集 | data_collector（19 维）+ event_stream + 宏观简报 | 每票 19/19 维有值 | 时效无 SLO |
| ② 结构化 | raw_event → canonical_event（相关性/事件簇） | 11-05 关键头条 15:04Z / 18:49Z 才可见（晚于 08:35 ET 批次） | 采到 ≠ 当时可见 |
| ③ 选池 | UE(glm-5.3) 主池 + bridge(deepseek-v4-pro) 提名 | 30 天 1538 行中 **1213 行是 supply_placeholder** | 占位行淹没漏斗 |
| ④ 推理 | DA 主 qwen3.7-max + 挑战者 deepseek-v4.1-flash | 325 条 instruction → **143 成交（44%）** | 不可复现、score 不可校准 |
| ⑤ 门槛链 | admission / entry_confirm / vwap / chase_cap / thesis_drift / 冷却 | 见 §2 | 拦截归因有黑洞 |
| ⑥ 执行 | 常规时段市价 + 延长时段执行器 | A10：regular n=684 **−$2,427** | 常规时段是主亏段 |
| ⑦ 出场/反馈 | SL/TP/保本/追踪/时间退出 + 归因 | 见 §3 | 盘后归因空段；契约违约 0 留痕 |

---

## §2 漏斗与真实门槛值（30 天，`playbook_instructions`）

```
row_kind=supply_placeholder  1213 行（executed=0）
row_kind=instruction          325 行 → executed 143（44.0%）
拒单构成：entry_confirm_reversed 80 / other 44 / thesis_drift 18
          no_trigger 17（归因黑洞，低估）/ vwap 13 / falsification_slope 10
```

**现役 admission 实际档位**（`config/unified_config.yaml: playbook.admission`，`enforce=true` + `enforce_at_execution=true`）：

| 键 | 值 | 说明 |
|---|---|---|
| `min_notional_usd` | **2500** | 2026-11-05 用户裁「恢复供给启用」由 5500 下调；**不要再以"名义太小"为由反向调整** |
| `max_fee_bps` | **11.54** | 逐笔事前往返费率上限（fee_model 同源）——这才是经济性真闸 |
| `min_entry_price` | 5.0 | 挡 OTC 仙股撞佣金地板 |
| `max_sl_pct` | 8.0 | 宽止损 = 同风险预算下名义更小 ⇒ 费率更高 |
| `max_open_risk_pct` | 4.0 | 组合层在险总帽（2.5% 时砍掉 87 笔、其中 87 笔合计 +$785 ⇒ 过紧） |
| `min_confidence_buy/short` | **0** | 2026-11-05 已裁「conf 没有判断力」⇒ 结构性退役 |
| `risk_cap_usd_per_trade` | **0** | 已证伪回退（缩仓使费率 7.3→26.9bps，符号反了） |

**其它在役门**：`fee_gate`（费 ≤ 预期毛利 25%，rr_mult 2.0）、`chase_cap_enforce`（禁追 0.5%）、
`premarket_invalidation_hard_gate`（现价/昨收 < −5% 拒）、`same_day_hard_stop_cooldown`、
`entry_archetype_routing`（**关闭**）、`entry_confirm`（60s 复核 + TTL）。

---

## §3 经济结构（A10，n=765）

| 指标 | 值 |
|---|---|
| 净额 / 毛额 / 费用 | **−$1,961.00** / **+$2,279.51** / **$4,240.44**（费/毛 **186%**） |
| 胜率 / 赔率 | 63.1% / payoff **0.52** |
| avg win / avg loss | +$30.54 / −$59.27 |
| 中位单笔 | +$5.25 |
| top10 / bottom10 | +$2,502.92 / −$3,532.41 |

**exit_reason（净额升序，节选）**

| 退出 | n | 净额 | 均值 |
|---|---:|---:|---:|
| SL_INITIAL | 41 | −5,981.13 | −145.88 |
| OVERNIGHT_GAP_STOP | 14 | −4,530.57 | −323.61 |
| MFE_STALL_EXIT | 31 | −1,352.64 | −43.63 |
| TIME_EXIT | 36 | −1,195.16 | −33.20 |
| DEAD_SIGNAL_EXIT | 53 | −271.67 | −5.13 |
| SL_BREAKEVEN | 294 | +1,097.49 | +3.73 |
| PRE_CLOSE_EXIT | 54 | +1,598.05 | +29.59 |
| SL_TRAIL_LOCK | 196 | **+8,753.63** | +44.66 |

**维度切片**

| 维度 | 桶 | n | 净额 | 均值 |
|---|---|---:|---:|---:|
| regime | risk_on | 360 | −1,834.70 | −5.10 |
| regime | cautious | 304 | +15.02 | +0.05 |
| regime | RANGE | 31 | +174.80 | +5.64 |
| regime | risk_off | 12 | −189.98 | −15.83 |
| 方向 | LONG | 601 | −2,282.13 | −3.80 |
| 方向 | SHORT | 164 | **+321.13** | +1.96 |
| 时段 | regular | 684 | **−2,427.27** | −3.55 |
| 时段 | premarket | 26 | −370.95 | −14.27 |
| 时段 | afterhours | 9 | +350.35 | +38.93 |
| MFE | 全样本 | 765 | 中位 **0.60%** | p25 0.36%；**<0.2% 占 19.5%** |
| 名义额 | 全样本 | 765 | 中位 **$5,618** | p25 $3,880 / p75 $5,858 |

**读数**：
- `risk_on` 是最大亏损桶（−1,835 = 全部净亏的 94%），而系统语义把 risk_on 当"放开做多"。**这是观察，不是因果**；至少说明"regime 高 ⇒ 多做"不是安全默认。
- 常规时段是主亏段；**延长时段 n=26/9 太小，不得据此下"延长时段赚钱"结论**（见 §10 更正②）。
- 费率地板：`commission = max(min($0.01×股数, 0.3%×额), $1.99)/腿`，qty≤199 恒付 $1.99 ⇒ 名义 $5,618 时往返 ≈7.1bps 起，`max_fee_bps=11.54` 已在拦更差的笔。**剩下降费用的路只有：少笔数换大单笔 / 提高毛利**（压低佣金档位=券商侧，不可控）。

---

## §4 信息输入层：三条缺口

1. **无市场级/板块级横截面**。DA 看到的是"个股 19 维 + regime 标签 + VIX + SPY>MA20"，**没有** QQQ/SMH/IWM 相对昨收、breadth、半导体分位。⇒ "今天该不该做多"这种**组合级判断无法被表达**，模型只能逐票答 BUY（11-05 的 BUY 11 就是这样来的）。
2. **时效无 SLO**。全类型 36h 窗：P50 **18.3 分钟**、P90 **487.5 分钟**、**30.84%** 超 60 分钟；11-05 FINNHUB 单源 P50 **89.8 分钟**、47/68 超 60 分钟。系统没有"过期信息不得使用"的机制。
3. **regime 只在开盘前算一次且用昨收**。11-05 09:00 ET 算出 `cautious / VIX 13.91 / score=4`，当天实际 −4.41%（09:55 破 −1.5%，收盘 −4.41%）。它不是"算错"，是**之后没再更新**。

---

## §5 推理链层：三条缺口

| 现象 | 证据 | 影响 |
|---|---|---|
| **不可复现** | 同输入、温 0、12 次：SELL_SHORT **11** / SKIP 1；score **{6.0, 8.0}** | 单次方向/评分变化 = 噪声；任何 A/B 必须 N≥10 并给方差基线 |
| **score 不可校准** | 6 与 8 无对应历史胜率/期望；分桶（<50 … ≥80）非单调（≥80 桶胜率 31.6%） | 所有"按 score 分档调参"目前是盲调 |
| **契约违约不可观测** | 离线复现 2/12 `generic_claim: bear_case 无数字锚点`；生产 325 条 instruction 的 `validation_errors` 记录 **0** | 无法做质量回归；"论据无数字锚点"这类漂移只能靠人眼 |

补：换档实测（`deepseek-v4.1-flash` 替主模型）在 16k/300s 预算下有效卡 **4/10**，6 次 `finish_reason=length`（completion 精确顶 16000），主调用 reasoning **10.5k–15.6k vs qwen 3.2k–5.3k**。⇒ **不换**；失败会静默回落 SKIP，在生产里伪装成"策略保守"。

---

## §6 入场机制：真正的两个错配

1. **入场质量**：MFE 中位 0.60%、**19.5% 的交易 MFE<0.2%** ⇒ 约 1/5 的成交从一开始就没有有利偏移，之后靠保本止损（294 笔 +$3.73/笔）"擦平"。这不是阈值问题，是**入场时点/形态问题**。
2. **形态路由关闭**：`entry_archetype_routing_enabled=false` ⇒ `PULLBACK_LIMIT` / `FADE_SHORT` 这类"低吸"指令仍以市价追。ADR-035 的预注册判据早已写好，开关没打——这是"**知道根因却没启用**"的典型。
3. **`entry_confirm` 拦了 80 条（最大拦截量），但无反事实结算**：`data/shadow/entry_confirm_forward.jsonl` 仅 8 行、无 settlement ⇒ 它到底救人还是误杀，**目前无数据可判**。

---

## §7 已被裁定、**不得重开**的轴（横跳禁令）

| 轴 | 裁定 | 依据 |
|---|---|---|
| 大盘普跌日禁做多 | **关闭** | DEB-20260924：71 日 / 789 条；最普跌桶毛收益最高（+1.059%） |
| 入场时点指数急跌禁做多 | **关闭（unpowered）** | 全窗 ≤−1.5% 仅 n=2/1 日；≤−0.5% 日聚类 CI 含 0；需 1300+ 交易日 |
| 全局 `RISK_OFF` 禁多 | **否** | 会连唯一正贡献的空头一起禁（A10 SHORT +$321） |
| 缩仓（`risk_cap_usd_per_trade`） | **证伪回退** | 缩仓使费率 7.3→26.9bps，净额符号反了 |
| `min_confidence` 门槛 | **退役（0）** | 2026-11-05 用户裁：IC(conf,$pnl)=+0.043、conf<40 占 96%，无分层 |
| `min_notional_usd` 上调 | **否** | 2026-11-05 用户裁 5500→2500 恢复供给；经济性由 `max_fee_bps=11.54` 管 |
| 换模型当解药 | **否** | 见 §5 |

---

## §8 改进点（按 ROI 排序）

**P1 · score 校准表（离线，零行为变更）**
按 score 桶统计历史胜率/期望/样本量，产出 `score → E[pnl]` 映射。**解锁条件**：任何 score 相关阈值讨论都必须在这张表之后。
判据：分桶单调性（Spearman）+ 每桶 n≥30 + 校准后 ≥2 桶的期望差 CI 不含 0。否则结论记"score 不可用"。

**P2 · 入场形态与拦截结算（有预注册依据）**
① `entry_archetype_routing_enabled` 按 ADR-035 判据启用（限价 + TTL，消灭"低吸变追高"）；
② `entry_confirm` 补 settlement（弃单后 +5m/+10m 反事实 + 结算入库），给出该门的净效果 CI。
判据：结算 n≥100 且反事实净额 CI 不含 0 才动该门阈值；未过则维持现状并停止讨论。

**P3 · `market_premise` 字段（离线 A/B，不作为门）**
DA 契约新增组合级前提字段（"今日市场环境：允许/限制/禁止做多" + 依据：指数相对昨收、breadth、板块分位），
**只作为 prompt 输入与事后归因，不驱动任何 gate**。
判据：同输入 N≥10 复跑下，`market_premise` 与最终方向的一致率 ≥80%，且离线回放（11-05 等 5 个冲击日）能产出"限制/禁止"标签 ≥4/5；满足才讨论是否让它影响下单。

**不做的四件**：见 §7 全表。

---

## §9 复现

```bash
cd /opt/ftseek/FTSeek-US
venv/bin/python3.11 scripts/audit_full_chain_20261106.py          # §2/§3 全部数字
venv/bin/python3.11 scripts/audit_full_chain_20261106.py --days 30 # 漏斗窗口
# 事件时延（§4.2）
sqlite3 data/event_stream.db "select count(*), round(avg(published_to_seen_ms)/60000.0,1) from canonical_events where first_seen_at >= '2026-11-01';"
# DA 重复性 / 换档（§5）
venv/bin/python3.11 scripts/da_repeatability_probe.py --raw-root data/da_checkpoint/2026-11-05 --symbol AMD --runs 10 --output-dir data/shadow/event_intelligence/<new_dir> --execute
venv/bin/python3.11 scripts/da_model_swap_probe.py --mode repeat --main-model deepseek-v4.1-flash --runs 10 --force-max-tokens 16000 --force-timeout 300 --output-dir data/shadow/event_intelligence/<new_dir> --execute
```

---

## §10 更正记录（本文件作者自己的两处）

1. **更正①**：初版口头结论称「延长时段赚钱、常规时段亏钱」。A10 全窗复核：`premarket n=26 −$370.95`、
   `afterhours n=9 +$350.35`——**延长时段样本太小且方向不稳**，不能下"赚钱"结论。稳健的只有一条：
   `regular n=684 −$2,427.27` 是主亏段。
2. **更正②**：初版把 confidence 分桶（<50 亏 −$1,896 / ≥70 赚 +$827）当作"低置信区该设门槛"的证据。
   这与 2026-11-05 已裁定「conf 无判别力（IC +0.043、conf<40 占 96%）」冲突，且我的分桶未控制方向/时段/regime。
   ⇒ **不得据此重开 conf 门**；若要再议，必须先做控制混杂后的边际效应检验。本文件 §7 已把该轴列入横跳禁令。