# DEB-20260930-skew-gate-vision-gap: 偏斜门视野盲区 (conf 倒置真机制) — 提案待裁 + RFC-121 撤回之撤回

- **状态**: 提案 (阶段 0 只读影子已可实施; 阶段 1 行为变更**待用户+复查 agent 双裁**)
- **目标读者**: 裁决者=用户; 复查者=第二 agent (只看本文档+跑命令即可独立证真/证伪); 执行者=裁决后的我
- **一句话目标**: 偏斜门 (`_apply_portfolio_guardrails`) 判定时**只看到本批 bridge 侧候选, 看不到同日 DA skill 直写行** → 用残缺分母算占比 → 9/30 实测 86% 超限砍掉 AMD/HPE(conf=70), 而当日并集 9B/4S=69% 根本不超限; 同时 conf=35 的 DA 直写单 (COIN/SNDK) 绕过本门照常成交 = conf 倒置真机制。
- **前置**: RFC-121 已撤回 (day-scope prior 方案) — **本 DEB 部分推翻该撤回的归因** (见 §9 自曝)。

---

## 1. 问题陈述 (含普查实测)

### 1.1 两条写库路径 (DB schema 指纹实证)

`raw_instruction_json` 二分 (9/30 全 13 行实测):

| 通道 | schema 指纹 | 走 guardrail? | 9/30 实例 |
|---|---|---|---|
| FTSeek `run_playbook_generate`→`_build_instructions_from_votes` | keys: `entry_price_source/atr_source/da_thesis/admission` | **走** (pipeline.py:2018 唯一调用点) | AMAT/LITE/TSLA/META/AMD/HPE/HOOD (7条) |
| DA skill Stage6b 直写 (`_pb_db.save_instruction(_inst)`, skill pipeline.py:2076) | keys: `source:"deep_analysis"/ue_direction/origin/entry_strategy` | **绕过** (grep skill 全文零 guardrail 调用) | KMX/BE/JBL/COIN/SNDK/ARM (6条) |

普查命令与输出 (E1-E3, 见 §5)。`_apply_portfolio_guardrails` 全仓**仅一个调用点** (pipeline.py:2018), 即 generate 路径; skill 直写、topup 路径均不经过。

### 1.2 9/30 时序地面真值 (日志+DB 交叉)

```
20:20-20:34 CST  DA skill 直写 6 条入库 (decision_id 时间戳 DA-202609302020xx~2034xx)
20:35:31 CST     FTSeek generate 批 UPSERT 13 行 (created_at 同秒批 = UPSERT 重写时刻, 非 trim 时刻)
21:05:55 CST     [LLMStrategyLoop] WARNING: 方向偏斜 86% 超限, 2 条信号被降级为 SKIP
                 = 6B/1S 视野 → 砍 AMD(conf=70)+HPE(conf=70); updated_at=20:02:04 UTC 佐证
21:13:26 CST     其余 11 行 updated_at (执行层轮次)
```

**86% = 6B/1S**: guardrail 当时视野只有 bridge 侧 7 条 (AMAT/AMD/HOOD/HPE/LITE/META=BUY 6 + TSLA=SHORT 1)。DB 里**先入库的 6 条 DA 直写行 (3B/3S) 不在视野**。当日并集 = 9B/4S = **69% ≤ 80%, 根本不该触发**。

### 1.3 为什么 DA 直写行不在 guardrail 视野

不是"没读到 DB", 是**结构上不可能读到**: guardrail 的输入是 `_build_instructions_from_votes` 的返回值 (本批内存对象), 从不查当日 DB。直写行只存在于 DB。⇒ **视野盲区是确定性的**, 不是竞态。

### 1.4 conf 倒置的完整因果链 (原报告 §3 现象为真, 归因需修正)

1. DA 直写单 (conf=35: COIN/SNDK) **绕过偏斜门** → 无条件入库;
2. bridge 侧高 conf 单 (conf=70: AMD/HPE) 进偏斜门, 门用**残缺分母** (6B/1S=86%) 判超限;
3. 砍序 = conf 升序 → 但本批 6 条 BUY 里 conf=70 有 5 条 (AMAT/AMD/HOOD/HPE/LITE/META), 并列时**砍谁由列表序决定** (9/30 实际砍了 AMD/HPE, 留下了 AMAT/LITE/META — 同为 70);
4. 净效果: 低 conf 直写单 100% 存活, 高 conf bridge 单被残缺分母误杀 → **conf=70 被砍、conf=35 成交**。

原报告说"批次局部≠组合全局"**方向对**(门视野确实小于组合), 但机制不是"多批次时序", 是"**双通道写入, 门只看得到一条**"。

---

## 2. 非目标

- **不动 DA 直写绕过 G6/EdgeShadowGate 的既有刻意设计** — pipeline.py:3681 注释明确"这是当前刻意设计, 非疏漏"(confidence 未校准, IC=−0.021); 本提案只修偏斜门的**分母完整性**, 不给直写单加质量门。
- **不改 80% 阈值**、不改砍序 (conf 升序)、不改 sector 门 (同样有盲区, 但 9 月零触发, 无实证伤害, 不仓促扩面 — 见 F3)。
- **不做 day-scope prior 跨批累计** (RFC-121 原方案) — 真病灶是视野盲区, prior 累计是它的特例修法且已证伪其独立性 (见 §9)。
- 不新增台账/新 cron/新表。

## 3. 设计树自问自答

**Q1: 这个改动会多出单/多赚钱吗?**
A: 会多出单 (9/1-9/30 回放: 实砍 45 → 修后仍砍 26, **救回 26 条, 全部 conf=70**, 反事实毛额 +1,943, entry→收盘口径, E8)。但这不是"多赚钱"承诺 — 毛口径、无费、无退出策略、n=26。真实收益主张是:**修尺子** — 门恢复对它声称要度量的东西 (当日组合方向占比) 的正确度量, 附带解除对高 conf 单的系统性误杀。

**Q2: 新失败模式是什么?**
A: ① **DB 读失败** → fail-open 退化现行行为 (视野=本批), 宁维持现状不误放行; ② **直写行含终态作废** → prior 过滤 INVALIDATED_SKIP_REASONS; ③ **同日重跑 generate** → UPSERT 语义下行已存在, prior 会把自己的上一次产出算进去 → 需按 symbol 去重 (本批 symbol 从 prior 剔除, 防双计) — 这是 RFC-121 原实现**没做对**的点, 本提案 E6 给出正确算法; ④ **sector 门盲区仍在** → 显式登记为已知残留 (F3), 不在本批扩面。

**Q3: 与已回滚/已证伪方案 (RFC-121) 什么关系?**
A: RFC-121 撤回结论"day-scope 是 no-op"**基于错误前提** (created_at 当 trim 时刻 → 误判 45/45 全在首批 prior=0)。实测: trim 发生在 generate 重写批 (13:05), 此时 DA 直写 6 行**已在库** → prior 非零, day-scope 有效。RFC-121 的**实现骨架** (prior 并入分母+fail-open+symbol 去重) 恰好是本提案修法, 但 RFC-121 的撤回文档必须补"撤回之撤回"更正 (§9), 防止后人读到"day-scope=no-op"就放弃正确方向。

**Q4: 为什么不直接把 DA 直写也塞进 guardrail?**
A: 直写发生在外部 skill 进程 (Stage6b), 时序上先于 FTSeek generate; 让 skill 调 FTSeek guardrail = 跨进程耦合+循环依赖。最小修法 = FTSeek guardrail 在判定时**补读当日 DB** (它本来就运行在直写之后)。源头过滤原则在这里的正确形态是"分母用真源 (DB 当日全量)", 不是"把门复制一份到写入侧"。

**Q5: 砍序并列 (5 条 conf=70 砍谁) 要不要修?**
A: 不修, 登记为观察项 (F4)。并列砍谁在统计上无差 (同 conf 同方向), 但 9/30 留下 AMAT/LITE/META 成交 +44/+84/+39、砍掉 AMD/HPE (+9/-135 反事实) 提示列表序≈scan 序有隐藏信息 — 无 n 支撑, 不动。

**Q6: 反方 — 会不会"视野盲区"其实是刻意设计?**
A: 查了: pipeline.py:3681 对 G6/EdgeShadowGate 的绕过有显式"刻意设计"注释, 但对**偏斜门盲区零注释零裁决** — 偏斜门 docstring (2026-06-25) 自称"组合级风险 guardrail", 组合级度量用单通道分母 = 实现与自称矛盾, 无设计依据 ⇒ 判缺陷而非设计。

**Q7: 修复后 9/30 那类日子的行为?**
A: E8 回放: 11 个 trim 日中 4 天 (9/2,9/3,9/4,9/25,9/30 共 5 天) 并集≤80% → **一条不砍** (救回 21 条); 6 天并集仍>80% → 照砍但少砍 (26→仍砍 26 中大部分, 救回 5 条)。真全局偏斜日 (9/21 93%, 9/22 100%) **砍数不变或更多需求** — 保护意图完整保留。

**Q8: 回放数字可信吗?**
A: 三重折扣声明: ①毛口径 (无费/滑点); ②entry→当日收盘, 无退出策略 (对照: 9 月成交人群同口径 −2,371 经真实退出后 −238, 退出贡献 ~+12/单, 救回单预期同量级缩水); ③救回 26 条中哪条被救依赖并列砍序假设 (取列表序前 k 近似)。E8 命令可复跑。

**Q9: 样本功效?**
A: n=26 救回单, t 未算 (毛额+假设砍序, 算 t 是伪精确)。只主张方向性: 26/26 全 conf=70、5/11 天整日零该砍 → 缺陷存在性是**确定性结构问题** (Q3 视野盲区数学上必然), 不依赖统计显著; 统计只用于量收益, 收益不作承诺。

**Q10: 为什么先影子 (阶段0) 而不是直接 enforce?**
A: 修的是**砍/不砍**的行为变更 (L2)。影子期零行为变更: 每轮 guardrail 时把"现行判定 vs day-vision 判定"双算落 jsonl (含 prior 构成), 30 天后用真实前向数据 (而非回放) 验证: ①分歧率 ②分歧单的实际成交与盈亏 ③DB 读失败率。这同时给复查 agent 提供独立数据源。

## 4. 模块线索表

| 模块 | 生产者 | 消费者 | 工件 | 幂等键 | 失败模式 | 回滚 |
|---|---|---|---|---|---|---|
| prior 补读 (阶段1) | guardrail 判定时同步读 `PlaybookDB.get_instructions_by_date(today)` | `_apply_portfolio_guardrails` 分母 | 无新工件 (读既有表) | 天然幂等 (只读) | DB 读失败→(0,0) fail-open=现行行为 | 删 `day_vision` 配置行或 revert 单 commit |
| 双算影子 (阶段0) | guardrail 每轮 | 无 (jsonl 供人/agent 读) | `data/shadow/skew_gate_vision.jsonl` | (date, batch_ts) 追加 | 写失败吞掉 (best-effort, 同 EdgeShadowGate 惯例) | 删 emit 调用 |
| 判定日志 | guardrail | 值班人/诊断报告 | 日志行 | — | — | — |

接线点唯一: `pipeline.py::_apply_portfolio_guardrails` 方向偏斜段 (2160-2178 现行行号)。

## 5. 证据表 (每条作者实跑)

| 编号 | 声称 | 复现命令 (cwd=/opt/ftseek/FTSeek-US) | 期望输出 (实跑值) |
|---|---|---|---|
| E1 | guardrail 全仓唯一调用点=generate 路径 | `grep -n "_apply_portfolio_guardrails(" app/playbook/pipeline.py \| grep -v "def "` | 仅 `2018:` 一行 |
| E2 | DA skill 直写零 guardrail | `grep -c "_apply_portfolio_guardrails\|guardrail" /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py` | `0` (sector_context 等无关词除外 — 用精确函数名 grep 为 0) |
| E3 | 9/30 被砍 2 条=AMD/HPE conf=70, trim@13:05 UTC | `venv/bin/python -c "import sqlite3;db=sqlite3.connect('data/ftseek_us.db');print(db.execute(\"SELECT symbol,confidence,updated_at FROM playbook_instructions WHERE date='2026-09-30' AND skip_reason LIKE '方向偏斜%'\").fetchall())"` | `[('US.AMD',70,'2026-09-30T20:02:04.708...'),('US.HPE',70,'2026-09-30T20:02:04.710...')]` (updated_at UTC 20:02=生成轮内 trim; 日志 21:05 CST=13:05 UTC 同轮) |
| E4 | 偏斜门日志 86% (6B/1S 视野) | `grep -h "方向偏斜.*超限" logs/ftseek_us.log* \| grep 2026-09-30` | `2026-09-30 21:05:55,408 [LLMStrategyLoop] ... 方向偏斜 86% 超限, 2 条信号被降级为 SKIP` |
| E5 | 当日 DB 并集 9B/4S=69% | `venv/bin/python -c "import sqlite3,json;db=sqlite3.connect('data/ftseek_us.db');rows=db.execute(\"SELECT direction,skip_reason FROM playbook_instructions WHERE date='2026-09-30'\").fetchall();B=sum(1 for d,s in rows if d=='BUY')+sum(1 for d,s in rows if d=='SKIP' and s and '多数方 BUY' in s);S=sum(1 for d,s in rows if d=='SELL_SHORT')+sum(1 for d,s in rows if d=='SKIP' and s and '多数方 SELL' in s);print(B,S,round(max(B,S)/(B+S),2))"` | `9 4 0.69` |
| E6 | 正确 prior=按 symbol 去重后 DB 已有活跃行 | §3 Q2③ 算法: prior_rows = get_instructions_by_date(today) 中 direction∈(BUY,SELL_SHORT)∧skip_reason∉INVALIDATED ∧ symbol∉本批 symbols → 9/30 = 6 行 (COIN/SNDK/JBL=BUY 3 + KMX/BE/ARM=SHORT 3) | day=(6+3)B/(3+1)S? 否 — 本批 bridge 6B/1S + prior 3B/3S = 9B/4S=69% |
| E7 | 11 个 trim 日被砍行全为 FTSeek schema、同日 DA 直写行更早入库 | 见本文档 §1 表 (脚本输出逐日: 被砍行 schema={'FTSeek'}, DA直写行数 0-16) | 11/11 天被砍行都是 FTSeek schema; 10/11 天有 DA 直写先行 (9/3 例外=0 行, 该日砍 2 由 bridge 内部 86% 触发) |
| E8 | day-vision 回放: 救回 26/45, 毛 +1,943 | 本文档随附脚本 (union 口径: 还原被砍行方向→全日占比→仍>80% 才砍 k 条→救回=实砍−k) | `合计: 实砍45 → day-scope仍砍26, 救回26, 反事实净额(entry→收盘,毛)+1943.02` |
| E9 | 既有"直写绕过=刻意设计"注释只覆盖 G6/EdgeShadowGate, 不覆盖偏斜门 | `sed -n '3676,3695p' app/playbook/pipeline.py` | 注释点名 "G6 confidence 门与 EdgeShadowGate", 零提及偏斜/sector guardrail |

## 6. 方案设计

**阶段 0 (只读影子, 零行为变更, 裁决通过即可落)**:
`_apply_portfolio_guardrails` 内, 偏斜判定处双算: 现行 batch-vision 占比 + day-vision 占比 (prior=get_instructions_by_date(today), 过滤终态+按 symbol 去重本批), 连同 (date, batch_ts, 两占比, 两判定, prior 构成, 本批构成) 追加 `data/shadow/skew_gate_vision.jsonl`。best-effort try/except 吞错。**判定仍用 batch-vision** (现行行为一字不变)。

**阶段 1 (行为变更, 需影子期数据+用户二次裁决)**:
判定分母切 day-vision; 日志同时打两占比; skip_reason 打标 `方向偏斜超限(day-vision B{x}/S{y})`; 配置开关 `playbook.guardrails.skew_day_vision: shadow|enforce|off` (默认 shadow), 回滚=改回 shadow 单行。

**口径铁律**: prior 只计 direction∈(BUY,SELL_SHORT) ∧ skip_reason∉INVALIDATED_SKIP_REASONS ∧ symbol∉本批 (防 UPSERT 自双计); DB 读失败→prior=(0,0)=现行行为 (fail-open, 宁维持现状)。

## 7. 决策依据最小充分集

| 项 | 状态 |
|---|---|
| ①固定样本 | 9/1-9/30 全 trim 日 (11 天 45 条), DB+日K缓存, 可复跑 (E8) |
| ②多窗口 | 单窗口 9 月 (系统 9/4 才 enforce, 更早无同源数据) — **不足**, 由阶段 0 影子补前向窗口 |
| ③对照臂 | 同日成交 conf≥65 BUY 对照 (26 条同口径 −475); "零判别臂"=现行行为影子期继续跑 |
| ④尾部 | 救回 26 条最差 −135 (HPE 9/30), 无单笔 >+700 主导 (最大 ARM 9/21 +563 属 9/21 但 9/21 救回=0 — 尾部集中于 9/2 +691/9 条) |
| ⑤费用后 | **未含** — 毛口径声明 (§3 Q8), 阶段 0 影子按成交口径回填 |
| ⑥机制量 | 视野盲区=确定性结构缺陷 (数学必然, 非统计推断); 分歧率将是影子期核心机制量 |
| ⑦功效 | n=26, 只主张方向性, 不算 t (伪精确) |
| ⑧预注册判据+回滚+复现 | 见 §8; E1-E9 全部可复跑 |

**凑不齐项 (②⑤) 的处置**: 正是阶段 0 影子存在的理由 — 缺陷存在性已由结构证据确立 (不需统计), 影子期补的是**收益量级与前向验证**, 不是缺陷真伪。

## 8. 预注册判据 (阶段 0→1 切换门, 先于实施固化)

影子期 ≥10 个交易日后裁决:
1. **分歧日占比**: day-vision 与 batch-vision 判定不同的日子 / 总 trim 触发日 — 预期 ≥30% (回放 5/11=45%); <10% 则收益太小, 关闭提案 (影子代码保留 record-only);
2. **救回单成交口径净额** ≥ 0 (影子期被 day-vision 放行且实际触发成交的单, 按真实成交 PnL 含费);
3. **DB 读失败率** <1% (fail-open 触发次数/总判定次数);
4. **阈值购物禁令**: 判据 1-3 读数出来后不得调整 80% 阈值或判据本身; 回滚触发=判据 2 为负且 <−$300。

**回滚法子**: 阶段 0=删 emit 调用 (1 处); 阶段 1=配置 `skew_day_vision: shadow` (1 行, 热更)。

## 9. 自曝 / 更正记录 (本 DEB 推翻的旧结论, 含我自己的)

| # | 旧结论 | 出处 | 更正 | 证伪源 |
|---|---|---|---|---|
| 1 | "批次局部≠组合全局, 偏斜门按批次算" | 9/30 核查报告 §3 | 现象真, 机制修正: 不是多批次时序, 是**双通道写入单通道视野** | E1-E7 |
| 2 | "day-scope prior 恒=0, 方案 A 纯 no-op, 救回 0" (RFC-121 撤回理由) | 本 agent 上一轮, commit 9373c0d28 | **撤回之撤回**: created_at=UPSERT 重写批时刻非 trim 时刻; trim 实际发生在 generate 轮 (13:05 UTC), 此时 DA 直写 6 行已在库, prior=6 非零; day-vision 对 9/30 实救 AMD/HPE | E3 (updated_at 20:02 UTC≠created 12:35)+E4 (日志 21:05 CST=13:05 UTC)+E6 |
| 3 | "被砍 45 条 100% 在当日首批" | 同上 | 表述本身对 (都在 generate 批) 但**推论错** — "首批"≠"无 prior": DA 直写行先于 generate 批入库 | E7 |
| 4 | "回放救回 21 条 +1,695" (RFC-121 原提案) | 本 agent, commit 3b1c24f41 | 订正为 union 口径**救回 26 条 +1,943** (原算法批次重构有误; union 口径 symbol 天然去重) | E8 |
| 5 | "conf 倒置根因=砍最低 conf 设计本身" (RFC-121 撤回文档 #4 初稿) | commit 9373c0d28 文档 | 设计没错 (conf 升序砍是 docstring 原意); 根因=分母残缺+直写绕过, 且并列 conf=70 时砍谁由列表序定 | §1.4 |

RFC-121 文档需追加"撤回之撤回"指针章 (正文原样留档, 按 review-system-design P7 纪律)。

## 10. 待决问题 (frontier)

- **F1 (用户裁)**: 阶段 0 只读影子是否即刻实施? (零行为变更, 按 SIM 期纪律可直接落; 但鉴于本 agent 上一轮刚发生"实施→自证伪→回滚", 主动交裁)
- **F2 (复查 agent 裁)**: §1.4 因果链与 E3 时序解读是否成立? 特别挑战: updated_at=20:02 UTC 与日志 21:05:55 CST(=13:05 UTC) 的对应关系 — 我的解读是 20:02 UTC=trim 落库时刻、13:05 UTC 日志=同轮 WARNING, **两者差 7 小时**, 需复查者独立判定哪个是 trim 真时刻 (若 20:02 UTC=次日轮次, 则 §1.2 时序需重排, 但"视野只有 7 条"不受影响 — 86%=6B/1S 只有 bridge 批能凑出)。
- **F3 (登记不修)**: sector 集中度门同患盲区 (分母同样只含本批), 9 月零触发无实证伤害 — 阶段 1 若 enforce, sector 门是否同批修? 建议: 影子期同点位双算 sector 分歧, 有读数再裁。
- **F4 (登记不修)**: 并列 conf=70 砍序=列表序 (§3 Q5), 影子期记录被砍/留存的 symbol 序, 攒样后看是否有信息。

## 11. 请复查者重点挑战 (点名)

1. **E3/F2 时序解读**: updated_at 20:02 UTC vs 日志 13:05 UTC 的 7 小时差 — 我判为"trim 在 generate 轮内先落库 (20:02 UTC=16:02 ET?)、WARNING 日志 21:05 CST 是另一轮重算"或时区换算有误; 请独立重排时序, 判定"guardrail 视野=6B/1S"是否仍成立 (86% 这个数字只能由 6B/1S 或 12B/2S 凑出, 请穷举当日所有子集)。
2. **E8 union 口径**: "还原被砍行方向→全日占比"假设被砍行若放行会以原方向入库且当日不触发其他门 — 请检查是否有被砍行同日已有其他 skip_reason 冲突 (如 chase_cap/漂移), 若有则该行的救回是虚的。
3. **§1.3 "结构上不可能读到"**: 请 grep 确认 guardrail 到 save 之间没有任何路径把 DB 当日行并进 instructions 列表 (我只查了 `_apply_portfolio_guardrails` 调用点唯一, 未查列表在调用前是否被别处扩充)。

## 12. 讨论记录

- R1 (2026-09-30, 本 agent): 原核查报告 §3 → RFC-121 day-scope 提案 → 实施 → 自测证伪 (基于 created_at 误读) → 回滚+撤回 (commit 9373c0d28) → 日志 86% 锚点触发二次核查 → 发现视野盲区真机制 → 本 DEB。
