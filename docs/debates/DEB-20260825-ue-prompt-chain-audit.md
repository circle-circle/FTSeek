# 议题：以 DA 第11–15轮分析模式为基准的 UE prompt 与链路深度审计

## 状态

- [x] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> 创建: 2026-08-25 (Claude 主导审计, 用户命题: "基于DA的分析模式, 针对UE的prompt和链路进行深度分析, 给出优化调整建议")
> 审计对象: `scripts/llm_universe_scan.py` (UE 全链) + `config/unified_config.yaml` universe_expansion 段 + 消费端 (tripwire/DA供料/scorecard)
> 方法基准: DEB-20260825-outcome-feedback-loop-revival.md 第 11–15 轮沉淀的 DA 分析模式
> **本轮零代码变更、零 prompt 变更, 全部方案待裁决。**

---

## 背景

### DA 分析模式清单 (从第 11–15 轮提炼, 作为本轮审计的七把尺子)

| # | 尺子 | DA 出处 | 一句话 |
|---|---|---|---|
| M1 | 内容正确性 | r11 | 死链路 / 证伪论据 / 过时统计 / 标签误导 / 语义冲突 |
| M2 | 执行性 ("写了有没有被执行") | r12 §C#5, r13 P1 | 输出契约校验+定向 repair; 词表归一防"假通过" |
| M3 | 信息量 ("输出还剩多少区分度") | r12 §C#1/#4, r14 P8/P9/P12 | score/conf 塌缩→锚定+代码映射; 仪式化输出检测; 伪独立 |
| M4 | 死内存治理 ("只写不读") | 本议题主线病灶1, r13 P3a, r15 P10 | 接线或删除; 到期裁决机制缺失是根因 |
| M5 | 测量地基 | r13 P6/P1a, r14 P10 | finish_reason/usage/prompt_hash/决策落盘含 SKIP |
| M6 | 口径纪律 | 口径七律#2, r13 §A | 版本戳切分; 选择效应; 预注册判据不挪门柱 |
| M7 | 归因卫生 | r12 §C#6, 各轮诚实边界 | prompt侧 vs 供给端分离; 不承诺提升期望 |

### UE 链路一页图 (实测接线)

```
cron 21:10 CST (ET 09:10 盘前, 周一~五)
  scan   : clue brief 组装(新闻+硬事件+8K/13DG+财报+内部人+峰群+板块佐证+负反馈表)
           → LLM(qwen-max, SYSTEM_PROMPT_V2 v2.4标) 浮现 surfaced[]
           → 归一化夹逼(Track A) → streak埋点 → 渠道归因 → self-critique影子(第2次LLM)
           → R5候选快照(signal_snapshots) → llm_universe_scan.jsonl
  inject : 读今日scan → cand过滤(min_conf0.5/非持仓/非DA已写) → conf降序取前30
           → 现价锚定(盘前pre_price) → 价格地板$5 → _compute_numbers(entry band+SL3%/TP6%)
           → DA优先防覆盖(RFC-066多行) → 同标的同方向3日冷却 → cap=50 → 写 playbook_instructions
  score  : (次日ET 16:40) scan_price→现价 fwd% → jsonl 追加
  push   : 钉钉汇总(tier语义标签, 已摘conf伪概率)
消费端  : ①auto_execute 统一执行轨道 ②DA skill _load_ue_short_candidates(SHORT供料,
           过滤 borrow_feasible=False + critique DROP) ③direction_hit_scorecard 三轴判分
           ④ue_tier_shadow_eval(周六周检) ⑤ue_midday_shadow_eval
```

---

## 数据与实证 (本轮全部本机复算, 可复核)

### E1. scan 侧行为分布 (窗口 8/10–8/24, 12 个交易日, `llm_universe_scan.jsonl`)

| 维度 | 实测 |
|---|---|
| surfaced | 83 条, tradeable 69 (LONG 42 / SHORT 27 / SKIP 14) — **多空供给均衡 (SHORT 39%)** |
| self_tier | HARD_EVENT 47 (68%) / MARKET_CORROBORATED 21 / THESIS_ONLY 1 |
| raw conf | 均值 LONG 0.731 vs SHORT 0.717 (几乎无差); 分布分散 (0.55–0.85 多点) |
| rubric_score | {3: 44, 4: 21, 2: 4} → **≥3 占 94%, 高位塌缩** |
| critique verdict | **DOWNGRADE 42 / DROP 24 / KEEP 3 → 攻击票 96%** |
| Track A 夹逼触发 | 0/69 (模型自律遵守良好) |
| streak≥3 | 5 行 (XOM 链为主) |
| prompt_version 字段 | **12 天全部 = "v2.4"** |

### E2. inject/执行侧 (playbook_instructions, decision_id LIKE 'universe_expansion%')

| 维度 | 实测 |
|---|---|
| 全史指令 | 125 行 (7/14–8/24); 非 SKIP 39 行 (8/10 后), executed 10 |
| **DB confidence 列** | **全史 {50: 119, 其余6条为cap上线前} → 列级信息死亡** (origin.raw_confidence 有留痕) |
| conditions_json / invalidation 列 | **39/39 全空** (UE 从不写这两个顶层字段) |
| invalidation_triggers (结构化) | **0 行** (r15 接线的结构轨对 UE 无输入) |
| SHORT 衰减 | surfaced 27 → 注入 9 (**18 只消失, 原因不可归因**, 见病灶6) |
| 执行账本全史 | 52 笔: BUY 34 笔 **−$437.34** / SELL_SHORT 18 笔 **+$30.71** / 合计 **−$406.63**, WR 26/52 |

### E3. tier 锚点自身未验证 (`data/ue_tier_shadow_eval_latest.json`, 8/22, settled n=102)

| tier | n | 超额均值 | t | IS/OOS | verdict |
|---|---|---|---|---|---|
| HARD_EVENT | 47 | +0.46% | +0.40 | **反号** (−0.08/+0.97) | NOT_SIGNIFICANT |
| MARKET_CORROBORATED | 38 | +0.56% | +0.57 | **反号** (+1.56/−0.44) | NOT_SIGNIFICANT |
| THESIS_ONLY | 3 | −3.06% | −2.86 | 同号 (n 不足) | NOT_SIGNIFICANT (n_gate✗) |
| UNKNOWN | 14 | −0.88% | −1.20 | 同号 | unknown_ratio 13.7% 在跟踪 |

→ HARD 与 MARKET 两档**无统计可辨区分度且 IS/OOS 双双反号**。8/21 A方案把 tier 定为"真正的置信度锚点"
(push 文案 📌>📊>💭 隐含层级序), 该序目前只有语义先验、没有数据背书。eval 脚本已预注册四关门与
"THESIS_ONLY 持续负→提案 inject 端过滤", 机制在位, 缺的是样本与到期裁决。

### E4. critique 影子三病交叉实证

1. **仪式化**: 66/69 攻击 (KEEP 仅 3) — DA 挑战者 missed_risks 107/107 的镜像病 (同构 r12 §C#4);
2. **首读倒挂**: direction_hit_scorecard.py:308 注释实锤 "影子判官区分度首读 2026-08-02 倒挂",
   live_readiness_report G1-3 门 "critique 无未解释倒挂" 就是为它设的;
3. **运行 33 天无裁决** (7/25 立项至今): 自评判据是"攒样本后切 DROP vs KEEP 的 fwd 对比, 显著则再裁是否实拦"
   — 攒样判分基建全在 (scorecard 三轴 + 快照回填), 缺的又是**到期裁决动作** (P7 指认的根因);
4. **双消费端政策不一致**: DA 供料侧过滤 critique=DROP (skill pipeline `_load_ue_short_candidates`,
   注释引 167 条历史 44 DOWN/22 DROP), 而 **inject 侧对同一 verdict 完全无视** (cmd_inject cand 过滤无此项)。
   同一个信号一个消费端信、一个不信 — 若它有效, inject 该用; 若无效, DA feed 也该停。

### E5. 中场影子 (midday) 判据进度健康

7 个交易日 / tradeable n=39, eval 输出 "样本不足 (需 ≥10天 且 n≥30), 继续攒样" — 预注册机制正常运转中, 不动。

---

## 实测台账 — 八个病灶 (对照七把尺子)

| # | 病灶 | 尺子 | 证据 (文件:行) | DA 对应 |
|---|---|---|---|---|
| 1 | **版本戳失真**: `PROMPT_VERSION="v2.4"` 常量, 但 SYSTEM_PROMPT_V2 内嵌注释显示 8/16 已做 v2.5 改动 (删死止损规则); 近 12 个交易日 scan 全部落库 v2.4 → **两个行为版本共用同一归因戳**, 新旧基线不可切分 | M6/M5 | llm_universe_scan.py:58 vs :114-117; E1 表 | r13 P6 prompt_hash (DA 曾同病: "prompt 常在未升版本号时被改") |
| 2 | **conf 三层失真**: ① DB confidence 列 119/125=50 (cap 结构性抹平, 列级死亡); ② inject 排序键/min_conf 门仍用单模型自评 conf — 8/21 A方案已在推送端承认其不可靠并摘除, 但注入端继续当真值用; ③ 替补锚点 tier 自身未过任何显著门 (E3) | M3/M6 | cmd_inject :1173-1179 (sort), :1245-1247 (cap); E2/E3 表 | r12 §C#1 score塌缩; r14 P8 (锚定量表+代码映射) |
| 3 | **critique 影子失衡+倒挂+逾期+双标** (E4 四条) | M3/M4 | :993-1021; scorecard:204-207,:308; skill pipeline:194-210 | r12 §C#3 H5双采样逾期结案; r14 P9 证伪官改造 |
| 4 | **invalidation 断链**: 模型每条都产 invalidation_ue (47/47), 但只进 origin blob; playbook_instructions.invalidation 列 39/39 空 (save_instruction 只认顶层 key, db.py:501, UE instruction dict 无此键); 结构化轨也无输入 → **tripwire_checker 文本轨+结构轨对 100% UE 指令双盲** | M4 | cmd_inject :1275-1303 (origin only); db.py:501; tripwire_checker 两轨口径 | **r15 P10 是本议题病灶1的第三次复现, UE 这里是第四次同构复现** |
| 5 | **输出契约无执行器**: schema_required 仅列级 {"surfaced": list}; per-item 靠归一化夹逼兜底 (好), 但 SHORT 缺 short_check 不拦不修 (provided 只标记零 enforcement)、SKIP 无 skip_reason 校验、scan 失败静默 return 1 不落任何记录 | M2 | :957 (schema_required), :822-827 (provided), :960-963 | r13 P1 validate&repair; r12 §C#5 |
| 6 | **inject 拒绝不可归因**: 价格地板→SKIP 行落库 (好), 但 无现价/:1207、entry_zone 无效/:1230、冷却/:1397、DA 优先/:1355 四类拒绝只留 stdout (log 文件有但非结构化) → SHORT 27→9 的衰减黑箱 | M5 | cmd_inject 各 continue 分支 | r12 P6 (SKIP 决策可见性是验证项前置) |
| 7 | **测量层与执行层叙事背离**: score/report 层 OOS 首日均值 +0.8% 显正 (REVIEW-20260824 表), 执行层全史净 −$406.63 — 影子评分口径 (scan_price 起算 fwd%) 与真实执行几何 (band 触发+SL/TP+费用) 的差距未在任何一处并置呈现, 单看 report 会高估本源 | M7 | cmd_score/cmd_report 全毛值口径; E2 账本 | r11 #5/#8 (性能行换费用后净期望) |
| 8 | **已决事项悬空**: streak≥3 硬闸 REVIEW-20260824 已裁 GO (预注册规则机械执行), 要求 8/25 ET 09:10 inject 前人工确认实施 — **该窗口已过, 今晨 cron 已跑, config 仍无 max_emergence_streak 键 = 默认 99 全放行**。GO 裁决正在静默失效 | M6 | config:1682-1751 无此键; 代码无过滤行 (REVIEW §五清单未落地) | r13 §E 生效时点纪律 (落地须盯执行日) |

## prompt 文本级审读 (尺子 M1, 对照 DA r11/r12 内容正确性清单)

| 项 | 发现 | 处置建议 |
|---|---|---|
| 多空对称性 | **与 DA 相反, UE 无此病**: 供给端 SHORT 39%、双侧 avg conf 几乎相等 (0.717 vs 0.731)。rule 8 SHORT 高门槛是结构性合理 (借券/逼空尾部), 不需要 DA P5 式大手术 | 不动。仅措辞级: rule 4 "多空对等"与 rule 8/10/11 单侧约束字面相抵, 建议改为 "多空机会对等浮现, 质量门槛各自对称" (纯语义收敛, 零行为) |
| 时效性论据 | rule 11 把 "(回调日 LONG 净正 / 追涨日 LONG 净负)" 的月度实证写死进 prompt 正文 — 这是下一个 "近14日命中29%" (r11 #3 同款过时统计): 月份翻页后论据失效但文字仍在误导 | 保留规则本体 (结构性约束), 删除括号内时效性实证引用, 或改 config 驱动定期刷新。列入 U7 附带项 |
| 正面清单 (明确不动) | ① 负反馈表注入 (v2.2 P3) = r11 #2 "历史胜负非方向先验"的正确同源设计; ② Track A 夹逼 = "能由代码判定的一律不交给模型"的先行范例; ③ evidence_ref 强制引证 + THESIS_ONLY 明示标签 = 可证伪化正确; ④ v2.5 已删死止损规则 (DEB-fullchain #23) | 保持 |

## 可选方案 (U1–U7, 按 信息增益/成本 排序; 全部标注冻结纪律兼容性)

| # | 方案 | 动作 (最小实现) | 修病灶 | 成本 | 风险/回滚 | 冻结兼容 |
|---|---|---|---|---|---|---|
| **U1** | **版本戳修复 (P6 移植)** | PROMPT_VERSION 升 "v2.5"; scan/inject 落库加 `prompt_hash` = sha1(system_prompt)[:12] (照抄 r13 `_prompt_fingerprint`) | 1 | 半小时 | 零行为。旧记录按日期切分即可 | ✅测量层 |
| **U2** | **invalidation 一行接线 (r15 模式)** | cmd_inject instruction dict 加顶层 `"invalidation": s.get("invalidation","")` → 自动落 invalidation 列 → tripwire_checker 文本轨即刻覆盖 UE (NL 解析器已在位); `horizon_hours` 同时透传 record-only (供未来 deadline 化) | 4 | 10 分钟 | 极低。宁漏不误杀原则下 checker 自带守卫; 回滚=删一行 | ✅接线不改行为 |
| **U3** | **critique 到期裁决 (P7 精神, 二选一)** | 用现有读数出结案报告: 候选级 ue_critique_candidates + G1-3 倒挂计数 + exec/unexec 对比。**若倒挂持续/无区分度 (n≥30) → 删除该调用 (省 1 次 LLM/日) 并同步移除 DA feed DROP filter (双端一致)**; **若区分度转正 → 按 P9 重写为证伪官** ("假设会亏, 找有数据出处的具体反证; 找不到就诚实 KEEP, 禁止编造") | 3 | 报告半天; 二选一实施 ≤半天 | 删除路径符合"做减法>打补丁"; 重写路径改模型所见需批准 | ✅减法/或报批 |
| **U4** | **输出契约轻量化 (P1 裁剪版)** | scan 侧 per-item 校验器 (只报告): SHORT short_check 必填且 downside_catalyst_expiry_hours>0 / SKIP skip_reason 非空 / 非 SKIP invalidation 非空; 违约走**单次 repair** (白名单仅 short_check/skip_reason/invalidation, direction/confidence/thesis 一律不接受补丁 — r13 零方向变更的结构性保障照抄); 开关 `UE_SCHEMA_ENFORCE=0` | 5 | 半天 | 违约率×1 小调用; 白名单外字段拒收 | ✅结构补全不改方向 |
| **U5** | **inject 决策落盘补齐 (P6 裁剪版)** | 新增 `data/shadow/llm_universe_inject.jsonl`: 每个 reject 分支追加一行 {date,symbol,direction,reject_reason:no_price/entry_zone/cooldown/da_priority,raw_conf,tier,streak}; price floor 已有 SKIP 行不重复 | 6 | 2 小时 | 纯观测。SHORT 衰减 27→9 首次可归因 | ✅测量层 |
| **U6** | **report 并置执行账本 (r11 #5 精神)** | cmd_report 头部加一行执行层实况: 全史/近30日 executed 笔数与净 PnL (pnl_net 金标准口径, 见 trade-records 净值约定 memory), 与 scan 层命中率并列呈现 — 让"影子显正/执行显负"的背离不可忽视 | 7 | 1 小时 | 纯展示 | ✅测量层 |
| **U7** | **streak 硬闸落地催办 (已决事项收尾)** + rule 11 月度实证引用清理 | 按 REVIEW-20260824 §五清单实施 (config `max_emergence_streak: 2` + inject 过滤行 + skill 侧 `_load_ue_short_candidates` 过滤行 + 4 态测试); **须用户明示确认** (评审文档原文要求人工确认, 且首个实施窗口已错过); 否决则明文关闭。附带: 删 rule 11 括号内月度实证引用 | 8, M1 | 1 小时 | 回滚=config 改 99 | ⚠️已预注册裁决, 差人工确认 |

## 关键分歧 (待辩论/裁决)

1. **U3 的 critique 是删还是造?**
   - 删除派: 33 天倒挂+96% 仪式化攻击, 它已经是"每天一次的表演调用"; DA feed 侧 DROP filter 建立在无效信号上同样该撤。
   - 重造派: 攻击方向没错, 错在无锚定的自由发挥; P9 式重写成本低于重建一套审查机制。
   - 可验证方式: 先出结案报告 (exec/unexec × KEEP/DOWN/DROP 四桶 + 倒挂计数), 数据说话。
2. **conf 排序键动不动 (病灶2②)?**
   - 不动派: cap=50 与排序是既有执行行为, 9 月冻结期不动; raw_confidence 留痕已够分析。
   - 动派: 排序键用已被本系统自己承认不可靠的信号, 是"知道尺子坏还用它量"。最小替代 = tie-break 加 (tier_rank, rubric_score) 或代码派生 sort_key。
   - 本轮倾向: **冻结期不动 live 排序**, 仅 U1 落 sort_key 留痕; 冻结期满后凭攒样数据再裁 (预注册见下表)。
3. **U2 接线后首例核销的信任策略**: r15 §E 先例要求首例命中人工复核一次再信任 — UE 指令同样适用 (checker 会开始把"已失效未执行的 UE 指令"标跳过, 属新行为路径)。

## 预注册判据 (挪门柱防护)

| 方案 | 判据 | 窗口/样本 | 不达标动作 |
|---|---|---|---|
| U1 | 新增扫描全部携带 prompt_hash; 后续对照一律按 hash 切分 | 即时 | — |
| U2 | 30 天内 UE 指令出现 ≥1 次 TRIPWIRE_INVALIDATED 或审计行覆盖 UE (计时起点=接线上架日, r15 §D 同款) | 30 天 | 仍 0 → UE invalidation 文本质量不足以支撑 NL 解析, 升级结构化 triggers 提案另议 |
| U3 | 结案报告四桶 exec n≥30; DOWN/DROP 与 KEEP 的 T+1 hit 差 <10pp 或仍倒挂 → 删; 差 ≥10pp 且方向正确 → P9 重写 | n≥30 | 报告本身即产出, 无悬空 |
| U4 | short_check provided 率与 skip_reason 非空率升至 100% (repair 后), repair 未改变任何 direction | n≥30 scan | 不升 → 判定为模型能力上限, 改为降档记录另议 |
| U5 | 连续 10 交易日每笔 reject 可归因; SHORT 衰减分解首次成表 | 10 交易日 | 缺行 → 修落盘点 |
| U6 | 展示项, 无判据 | — | — |
| U7 | 实施次日起 streak≥3 浮现注入数 = 0; 或用户否决后议题内明文关闭 | 实施→关闭 | — |
| (冻结期满备用) | sort_key=tier/rubric 派生排序 vs raw_conf 排序的 executed 净差对比, 双窗同号才切换 | n≥30/臂 | 不同号 → 保留现状 |

## 不建议做的 (对抗性自审, 防后续 Agent 重复提案)

1. ❌ **不动 entry_enabled / cap=50 数值 / max_targets_per_day=30 / 3日冷却 / $5价格地板 / min_confidence** — 既有门禁, 9 月冻结;
2. ❌ **不给 UE 加双模型共识/挑战者辩论链** — UE 定位是浅分析高吞吐发现层, 深度由 DA 承担; 复制 DA 辩论结构 = 重复建设且 token 成本×N;
3. ❌ **不按 tier 字面层级升级 HARD_EVENT 权重/优先级** — t=0.40 未过线, ue_tier_shadow_eval PASS 前禁止 (其自身 prereg 已写明);
4. ❌ **不做 prompt 多空对称化手术 (DA P5 全套)** — E1 实测 UE 供给端无此病, 症状不同不能套药方;
5. ❌ **不加新论据维度/新数据段** — 七轴零分离教训 (r3/r7) 同样约束 UE;
6. ❌ **本轮不实施任何方案** — 全部待用户裁决; U7 尤其需要明示确认 (评审文档的人工确认条款)。

## 决策

- 选择方案: **维持待用户裁决**; 本轮新增 prompt 级优化建议 `P1–P11` (见第 2、3 轮), 与既有 `U1–U7` 形成"代码链路 + prompt 工程"双轨方案集。
- 推荐裁决顺序:
  1. **今日可合 (零行为风险)**: `P1` prompt 版本戳修复 + `P2` rule 4 措辞收敛 + `P3` rule 11 时效引用删除 + `P7` 输入源健康头写进 user prompt + `U2` invalidation 顶字段透传。五者合计 <1 小时, 只改测量/文本/输入感知, 不动任何执行行为。
  2. **dry-run 后合 (低行为风险)**: `P4`/`P5` 输出契约强化 + `P8` SYSTEM_PROMPT_V2 重组为 4 层 + pre-mortem + `U4` schema repair 裁剪版; 先以 `UE_SCHEMA_ENFORCE=0` 跑 5 个交易日观测违约率, 无 direction 变更再开强制。
  3. **数据驱动裁决 (中等风险)**: `U3` critique 结案报告 → 按报告决定 `P6`/`P10` 证伪官重写/pre-mortem 改造或删除调用; `P9` per-item schema + `U5`/`U6` 可归因落盘/执行账本并置随本批实施。
  4. **须用户明示确认**: `U7` streak 硬闸落地 (REVIEW-20260824 已裁 GO, 首个窗口已过, 不能再悬空) + rule 11 实证引用清理同步完成。
  5. **中长期 (依赖样本)**: `P11` 负反馈表错误类型归因并回注 prompt; 需 `U5`/`U6` 落盘 10 交易日以上数据。
- 决策依据: 以 DA r11–r15 七把尺子为基准; 按 prompt 工程五维（输入工程 → 结构工程 → 输出契约 → 推理控制 → 反馈闭环）逐层加固; 优先处理测量地基 (M5/M6) 与断链接通 (M4), 再处理内容正确性 (M1) 与执行性 (M2), 最后处理信息塌缩 (M3)。
- 保留空间: 所有方案与 9 月冻结纪律 (不切实盘/不动退出参数/不动既有门禁阈值) 无冲突; `U2` 是 r15 已批接线逻辑在同一链路的对称延伸; `P1–P3`/`P7`/`P8` 为纯文本/结构/测量层变更。
- 负责人: Claude (审计+方案, 待命实施), 用户 (裁决与 U7 确认)
- 截止时间: 建议 2026-08-31 终裁节点前 (与主议题同步); `U7` 若确认实施, 须不晚于下一个 ET 09:10 inject 窗口前完成配置与测试。

## 执行项 (待批准后)

### Prompt 工程级 (P1–P11)

- [ ] P1: `llm_universe_scan.py` `PROMPT_VERSION="v2.4"` → `"v2.5"`; scan/inject/jsonl 落库 `prompt_hash` = sha1(SYSTEM_PROMPT_V2)[:12]
- [ ] P2: SYSTEM_PROMPT_V2 rule 4 "多空对等" → "多空机会对等浮现, 质量门槛各自对称"
- [ ] P3: SYSTEM_PROMPT_V2 rule 11 删除括号内 "(回调日 LONG 净正 / 追涨日 LONG 净负)" 月度实证引用, 保留规则本体
- [ ] P4: SYSTEM_PROMPT_V2 rule 8 末尾追加 "SHORT 必须输出完整 short_check, 缺失任一字段视为格式违约"
- [ ] P5: SYSTEM_PROMPT_V2 rule 6 追加 "非 SKIP 标的 invalidation 不得为空; SKIP 标的须给出 skip_reason"
- [ ] P6: 若 U3 裁决保留 critique, 重写 self-critique 调用 prompt 为证伪官角色 ("假设 T+1 会亏, 找具体可核验反证; 找不到就诚实 KEEP, 禁止编造")
- [ ] P7: `_build_clue_brief` 在 user prompt 顶部追加 `[今日输入源健康]` 块, 显式标注缺失/降级源
- [ ] P8: 将 `SYSTEM_PROMPT_V2` 从 11 条平铺规则重组为 "角色 → 任务 → 4 层纪律 → pre-mortem 自检 → 输出格式"
- [ ] P9: 实现 per-item JSON schema 校验器（存在性检查 + 白名单 repair）, 替换当前 `schema_required={"surfaced": list}`
- [ ] P10: critique 二选一: A) 改为主调用内 pre-mortem; B) 保留独立调用但使用完全独立 system prompt
- [ ] P11: 负反馈表增加错误类型归因, prompt 中展示 Top-3 高频错误模式 + 最近 3 例

### 代码/链路级 (U1–U7)

- [ ] U1: PROMPT_VERSION→v2.5 + prompt_hash 落库 (scan/inject/jsonl 三处)
- [ ] U2: cmd_inject instruction dict 补顶层 invalidation 键 + horizon_hours 透传
- [ ] U3: critique 结案报告 → 按报告删除调用(+DA feed filter 同步) 或 P6/P10 重写
- [ ] U4: per-item 校验器 + 白名单 repair + UE_SCHEMA_ENFORCE 开关
- [ ] U5: llm_universe_inject.jsonl reject 落盘
- [ ] U6: cmd_report 并置执行账本净 PnL 行
- [ ] U7: streak 硬闸按 REVIEW §五实施 (须用户确认) + rule 11 实证引用清理

## 验证项

- [ ] U2: 30 天 UE 核销判据 (见预注册表)
- [ ] U3: 结案报告四桶数据齐全且结论唯一
- [ ] U5: SHORT 衰减 27→9 分解表首次成表
- [ ] 关闭条件: U1-U7 全部实施或明文否决; 病灶 1-7 逐条复核销案

## 讨论记录

### 第 1 轮 (2026-08-25, Claude 独立审计)

- 通读 `scripts/llm_universe_scan.py` 全文 (1837 行) + config UE 段 + 五个消费端 (tripwire_checker / skill pipeline 供料 / direction_hit_scorecard / ue_tier_shadow_eval / ue_midday_shadow_eval) + REVIEW-20260824 + 相关 git 历史 (RFC-066/audit-fix 系列)。
- 以 DA r11-r15 七把尺子逐条照射, 产出台账八病灶 (全部本机复算, 关键数字: conf 列 119/125=50、critique 攻击票 66/69、tier 双档 IS/OOS 反号、invalidation 列 39/39 空、prompt_version 恒 v2.4 跨两个行为版本、SHORT 浮现→注入 27→9、执行账本全史 −$406.63)。
- 诚实声明: ① UE 样本薄 (52 executed / 102 settled), 一切切片不下强统计结论; ② UE 的价值定位是宇宙扩张发现层, 本轮目标是**测量可信 + 断链接通**, 不承诺提升期望 (第 5 轮终判"净转正只剩换信号源"不变); ③ 病灶 5 强度弱于 DA 当年 (model_call 层 7/25 起已有 finish_reason/截断降速), 如实降级; ④ 病灶 8 不是新发现, 是已决事项的执行悬空, 按 M6 尺子必须点名。

### 第 2 轮 (2026-08-25, Claude 基于 DA 模式给出 prompt 优化建议)

本轮聚焦 **prompt 文本级调整与执行契约强化**, 不动既有门禁阈值/数值参数。建议按"先修测量地基 → 再清文本病灶 → 后判影子生死"的顺序分批实施, 全部与 9 月冻结纪律兼容。

#### 2.1 Prompt 文本级修改草案 (零行为变更或仅措辞收敛)

| # | 位置 | 当前文本/行为 | 建议修改 | 对应病灶/尺子 | 备注 |
|---|---|---|---|---|---|
| P1 | `PROMPT_VERSION` 常量 | `"v2.4"` | 升为 `"v2.5"`, 与注释标注的 8/16 行为版本对齐; 同时 scan/inject 落库 `prompt_hash` = sha1(SYSTEM_PROMPT_V2)[:12] | 病灶 1 / M6, M5 | 纯测量层, 不回溯旧记录 |
| P2 | rule 4 | "多空对等" | 改为 "多空机会对等浮现, 质量门槛各自对称" | M1 | 零行为, 仅消除与 rule 8/10/11 单侧约束的字面冲突 |
| P3 | rule 11 括号内 | "(回调日 LONG 净正 / 追涨日 LONG 净负)" | 删除该月度实证引用, 保留规则本体; 或改为 config 驱动定期刷新 | 病灶 8 附带, M1 | 防止月份翻页后论据变谎言 |
| P4 | rule 8 末尾 | "无具体下跌催化剂(仅'估值高'/'涨多了')的 SHORT 不得浮现" | 追加 "SHORT 必须输出完整 short_check, 缺失任一字段视为格式违约" | 病灶 5 / M2 | 不新增模型判断, 把已有要求从"建议"升格为"契约" |
| P5 | rule 6 末尾 | 仅要求输出 invalidation/horizon_hours | 追加 "非 SKIP 标的 invalidation 不得为空; SKIP 标的须给出 skip_reason" | 病灶 4, 5 / M2, M4 | 为 U2 接线提供 prompt 侧前提 |
| P6 | self-critique 调用 prompt (993-1000 行) | "逐条攻击其因果链最薄弱的一环" | 若 U3 裁决保留 critique, 重写为: "假设该标的今日买入/卖空后 T+1 会亏钱, 必须给出具体、可核验的反证(数据/事件/价格行为); 找不到就诚实输出 KEEP, 禁止编造 weakest_link" | 病灶 3 / M3 | 目标从"表演攻击"转向"可证伪审查" |

#### 2.2 执行契约与代码侧配套 (P4-P6 落地所需)

- **schema_required 扩展**: 当前仅校验 `{"surfaced": list}`。建议 per-item 增加"存在性"检查 (非正确性检查): `short_check` 在 SHORT 时必须为 dict 且含 `downside_catalyst_expiry_hours>0`; `invalidation` 在非 SKIP 时非空; `skip_reason` 在 SKIP 时非空。违约走单次 repair, 白名单仅限这三个字段, direction/confidence/thesis 一律不接受补丁 (复刻 DA r13 零方向变更保障)。
- **repair 失败处理**: 若 repair 后仍缺字段, 记录 `schema_repair_failed=true` 并降级该条为 SKIP, 不阻断其他标的注入。
- **inject 侧透传**: 在 `instructions` dict 顶层增加 `"invalidation": s.get("invalidation", "")` 与 `"horizon_hours": s.get("horizon_hours")`, 让 `save_instruction` 自动写入 `invalidation` 列, tripwire_checker 文本轨立即覆盖 UE (U2 一行接线)。

#### 2.3 不建议改动的 prompt 区域 (对抗性自审)

- 不引入 DA 式双模型辩论/挑战者结构 — UE 定位是浅分析高吞吐发现层, 复制 DA 辩论链 = 重复建设 + token 成本×N (与文档"不建议做"第 2 条一致)。
- 不调整 rule 8/10/11 的结构性约束强度, 只修措辞与失效引用 — 当前 SHORT 供给 39%、多空 conf 几乎对称, 说明约束本身运行正常。
- 不按 tier 层级提升 HARD_EVENT 的隐式权重 — ue_tier_shadow_eval 尚未过显著门 (t=0.40), 权重调整需等 eval 预注册判据触发。
- 不新增数据维度/论据段 — 七轴零分离教训 (r3/r7) 同样适用 UE, prompt 增量须受可证伪化与代码夹逼双重约束。

#### 2.4 推荐实施批次 (按风险与依赖排序)

1. **第一批 (纯测量/文本, 今日可合)**：`P1` + `P2` + `P3` + `U2` 代码一行接线。风险为零, 只改归因戳与措辞, 行为不变。
2. **第二批 (契约强化, 需一次 dry-run 验证)**：`P4` + `P5` + schema repair 轻量化实现 (`U4` 裁剪版)。先开 `UE_SCHEMA_ENFORCE=0` 观测一周违约率, 再决定是否硬拦截。
3. **第三批 (影子裁决, 依赖数据)**：`P6` (若保留 critique) 或删除 critique 调用 (`U3` 删除派)。必须先出结案报告 (exec/unexec × KEEP/DOWN/DROP 四桶 + 倒挂计数), 数据说话。
4. **第四批 (已决事项收尾)**：`U7` streak 硬闸落地 + rule 11 实证引用清理。须用户明示确认, 首个窗口已错过, 不能再拖。

#### 2.5 诚实边界重申

- 上述 prompt 调整**不承诺提升 UE 期望收益**; 核心收益仍是"测量可信 + 断链接通"。
- UE 样本量 52 executed / 102 settled, 任何基于子集的"显著性"结论都需谨慎; 新增字段与判据需按预注册表攒样后再裁决。
- critique 若重写为证伪官, 仍可能因 LLM 幻觉产生无效攻击; 须以 scorecard 三轴 + 快照回填为最终判据, 不以 prompt 自评替代。

### 第 3 轮 (2026-08-25, 从专业 prompt 工程角度重构 UE 链路真实性)

本轮把焦点从"文本改哪里"提升到"prompt 工程五维 + 链路闭环"层面, 目标是让 UE 不仅在单点输出合规, 更在**数据输入 → prompt 结构 → 模型输出 → 代码夹逼 → 执行反馈 → 测量归因**全链路上真实有效。

#### 3.1 UE 场景对 Prompt 工程的约束（先定边界，再谈优化）

| 维度 | UE 特征 | 对 Prompt 工程的 implication |
|---|---|---|
| **定位** | 宇宙扩张发现层，浅分析、高吞吐 | 不做 DA 式深度辩论；prompt 应聚焦于"浮现候选"而非"论证充分" |
| **输入** | 多源异构（新闻、硬事件、8K/13DG、峰群、市场佐证、负反馈） | 必须做信息 diet 与优先级显式化，否则模型被噪声淹没 |
| **输出** | 多空双向 + 宇宙外标的 + 需可交易 | 输出 schema 必须强约束，代码侧必须能验证可执行性 |
| **消费端** | inject / DA 供料 / scorecard / tier shadow / midday shadow 五端 | prompt 输出字段必须同时满足五端口径，不能仅服务单一消费端 |
| **测量** | 样本薄、日频低、方向混杂 | 任何 prompt 变更必须带版本/hash/输入源健康日志，否则无法判断"是 prompt 有效还是样本 noise" |

> 核心原则：**UE 的 prompt 不是"让模型更聪明"，而是让模型在有限上下文里输出可被代码验证、可被下游消费、可被事后归因的结构化信号。**

#### 3.2 当前链路在 prompt 工程上的真实有效性问题

1. **输入侧：信息 diet 已分层，但模型对"缺失"无感知**
   - `_build_clue_brief` 已按硬事件 → 事件标的 → 峰群 → 市场佐证 → 新闻分层，但未在 prompt 中显式告知模型"今日缺少某类输入"（如 8-K 失败、峰群为空）。
   - 结果：模型把"无新闻"当成"无机会"，或把陈旧新闻当新鲜新闻处理。

2. **Prompt 结构：11 条规则平铺，违反 LLM 注意力衰减规律**
   - SYSTEM_PROMPT_V2 把角色、任务、11 条规则、输出 schema 全塞进 system message；实测 rubric_score 94% 高位塌缩、critique 96% 攻击票，说明模型对后段规则（尤其是输出契约）的执行度下降。
   - 规则之间还存在字面冲突（rule 4 vs rule 8/10/11，P2 已修）。

3. **输出侧：schema 校验停留在 list 级，per-item 全靠代码夹逼兜底**
   - `schema_required={"surfaced": list}` 只保证最外层结构；per-item 字段缺失、类型错误、方向语义漂移，全部依赖 `_normalize_surfaced` 事后修复。
   - 这是"先污染后治理"，不是"prompt 工程里的输出契约"。

4. **闭环侧：critique 仪式化 + 执行反馈未回注 prompt**
   - self-critique 二次调用与主调用使用同一 system prompt，导致"攻击者"角色被 system prompt 里的"浮现者"角色污染（同一模型、同一角色、同一目标）。
   - 负反馈表已注入，但只有"推对/推错"，没有"为什么错"的归因，模型无法从错误中学习。

5. **测量侧：版本戳失真、缺少 prompt_hash、无输入源健康摘要入档**
   - 病灶 1 已详述；此处强调的是：没有这些地基，任何 prompt 优化都无法被验证。

#### 3.3 Prompt 工程五维优化建议（可增量实施）

##### 维度 1：Context Engineering（输入工程）——让模型知道"吃什么、缺什么"

建议把 `_build_clue_brief` 的输出增加一个**显式输入源健康头**，并直接写进喂给 LLM 的 user prompt（而非只打印日志）：

```
[今日输入源健康]
硬事件: N条 (源: 新闻关键词/财报日历/内部人/8-K/13DG)
事件标的清单: N只
盘前峰群: N组 (verdict=X)
市场佐证: 日期YYYY-MM-DD
新闻: N条 (近24h, 其中带标的K条)
负反馈样本: N条 (近7日)
缺失/降级: [8-K源失败 | 峰群空 | 内部人报告断更Md]
```

- **为什么有效**：LLM 没有时钟，也没有外部状态感知；显式告诉它"今日 8-K 失败"，它就不会把旧 8-K 当新事件引用。
- **为什么真实**：这与现有 `health` 日志同源，只是把同一信息同时喂给 LLM 和审计日志，零新增数据源。

##### 维度 2：Prompt Structure（结构工程）——把 11 条规则压缩为 4 层 + 1 个输出前自检

建议把 SYSTEM_PROMPT_V2 重组为：

```
角色：你是事件驱动型美股发现层分析师，只负责浮现候选，不做深度论证。

任务：读今日事件与市场佐证，输出受影响美股标的（含宇宙外），多空都要。

四层纪律（按优先级降序）：
1. 浮现纪律：可核验才浮现，不确定填 SKIP；证据强弱体现在 self_tier/confidence。
2. 证据纪律：HARD_EVENT 必须引用具体事件原文；MARKET_CORROBORATED 必须引用市场异动数据；THESIS_ONLY 必须明示"板块联想:"或"逻辑推演:"。
3. SHORT 纪律：SHORT 必须输出完整 short_check；无具体下跌催化剂不得浮现。
4. 输出纪律：非 SKIP 必须输出 invalidation/horizon_hours/expected_move_pct/evidence_checklist；SKIP 必须输出 skip_reason。

输出前自检（必须在最终 JSON 前在脑中完成）：
- 每个非 SKIP 标的，我能否说出"如果哪个事实发生，这个 thesis 就失效"？
- 每个 SHORT，我能否给出 downside_catalyst_expiry_hours？
- 每个 evidence_ref 为空，我是否已标 THESIS_ONLY 且 confidence≤0.60？

输出格式：严格 JSON {"surfaced": [...]}
```

- **为什么有效**：把 11 条平铺规则压缩为 4 层语义结构，并在输出前加一个轻量自检（不输出 CoT，只作为内部约束），可显著降低规则遗忘率。
- **为什么真实**：这是纯文本重组，不新增任何模型判断负担；所有约束已在代码侧有夹逼兜底。

##### 维度 3：Output Schema & Validation（输出契约）——从"list 级"深化到"item 级"

建议把 `call_llm` 的 `schema_required` 替换为更严格的 JSON Schema（如果底层支持），或在调用后增加一次**轻量 item 级校验器**（作为 prompt 工程的一部分，不是业务逻辑）：

```python
_PER_ITEM_SCHEMA = {
    "symbol": str,
    "direction": {"enum": ["LONG", "SHORT", "SKIP"]},
    "confidence": float,  # [0,1]
    "self_tier": {"enum": ["HARD_EVENT", "MARKET_CORROBORATED", "THESIS_ONLY"]},
    "evidence_ref": str,  # HARD/MARKET 非空由代码夹逼保证
    "invalidation": str,  # 非 SKIP 非空
    "horizon_hours": int,
    "evidence_checklist": dict,
    # SHORT 专属
    "short_check": {"borrow_feasible": bool, "downside_catalyst_expiry_hours": int, "squeeze_risk": int},
}
```

- 校验失败分两类：
  - **格式违约**（缺字段/类型错）：走单次 repair，白名单字段只补格式不补方向。
  - **语义违约**（如 HARD_EVENT 但 evidence_ref 空）：由现有 Track A 夹逼处理，不交给 LLM repair。

- **为什么有效**：把"模型应该输出什么"从文本建议变成可验证的契约；prompt  Engineering 里这叫"structured generation + contract validation"。
- **为什么真实**：与 P4/P5/U4 不冲突，只是从工程方法论层面说明 rationale。

##### 维度 4：Chain-of-Thought / Self-Critique（推理控制）——把"事后攻击"改为"事前预验"

当前 critique 是**事后二次调用**，角色被 system prompt 污染，导致仪式化攻击。

建议改为**主调用内的轻量 pre-mortem**：在 SYSTEM_PROMPT_V2 末尾追加：

```
在输出每个标的前，先问自己："如果该标的今日按此方向入场，T+1 亏钱的最可能原因是什么？"
把答案浓缩进 invalidation 字段；找不到具体、可核验的反证，就填 SKIP。
```

- **为什么有效**：把"外部攻击者"变成"内部预验"，避免角色冲突；同时把 critique 产出直接映射到已有 `invalidation` 字段，不新增消费端。
- **为什么真实**：如果模型仍编造 invalidation，代码侧 tripwire_checker 会在执行后核销时验证其是否可观测；prompt 侧负责"尝试诚实"，代码侧负责"验证是否真发生"。

> 备选方案（若仍想保留独立 critique 调用）：把 critique 的 system prompt 完全替换为独立角色，且**不与主调用共享 system prompt**。

##### 维度 5：Feedback Loop（执行反馈回注）——让 prompt 从"静态规则"变成"动态学习"

当前负反馈表只有 `symbol direction result`，建议增加**错误类型归因**：

```
- 2026-08-20 ORCL SHORT: -5.2% ✗ [错误类型: 事件已price-in / 重复浮现D6 / 无新催化剂]
- 2026-08-21 XOM LONG: -2.1% ✗ [错误类型: 追涨延伸 / entry区偏离]
```

并在 prompt 中按错误类型分组展示（不要全塞，只展示 Top-3 高频错误类型 + 最近 3 例）：

```
[近7日高频错误模式]
1. 事件已price-in (N例): 新闻 headline 看似强催化，但盘前涨幅已>5%。
2. 重复浮现无新催化剂 (N例): 同标的连续多日同方向，后续日无新增证据。
3. 追涨延伸 (N例): LONG 标的当日已涨>3%且无回踩证据。

请优先规避上述模式。
```

- **为什么有效**：把执行结果从"奖惩信号"升级为"可迁移的模式知识"，模型能在浮现端主动规避已知失败模式。
- **为什么真实**：错误类型可由现有 `playbook_instructions` + `trade_records` + `raw_instruction` 自动推导，不需要人工标注；预注册判据可以是"加入归因后重复错误率下降"。

#### 3.4 链路真实性保障：Prompt 工程必须与代码/测量/闭环联动

单独改 prompt 不能保证真实有效。建议建立以下**五层联防**：

| 层级 | 当前状态 | 建议补强 |
|---|---|---|
| **L1 输入真实** | 多源已接入，健康日志已打印 | 把健康头写进 user prompt；缺失源显式标注 |
| **L2 Prompt 真实** | 11 条规则平铺，版本戳失真 | 重组为 4 层 + pre-mortem；修复版本/hash |
| **L3 输出真实** | list 级 schema，per-item 靠代码夹逼 | item 级 schema + 白名单 repair |
| **L4 执行真实** | invalidation 未透传，tripwire 对 UE 双盲 | U2 接线 + tripwire 文本/结构双轨覆盖 |
| **L5 反馈真实** | 负反馈表只有结果，无错误归因 | 增加错误类型归因并回注 prompt |

> 只有 L1–L5 同时到位，prompt 优化才具备可验证性；否则任何"感觉变好"都是幻觉。

#### 3.5 与 P1–P6 / U1–U7 的映射

| 本轮建议 | 对应 P/U | 说明 |
|---|---|---|
| 输入源健康头写进 user prompt | 新增，可并入 U5 观测 | 零行为风险，增强模型对缺失的感知 |
| SYSTEM_PROMPT_V2 重组为 4 层 + pre-mortem | P2/P4/P5/P6 的底层 rationale | 文本级，不冲突 |
| item 级 schema + 白名单 repair | P4/P5 + U4 | 工程实现层面 |
| critique 改为 pre-mortem 或独立 system prompt | P6 | 更根本的角色冲突解决方案 |
| 负反馈表增加错误类型归因 | 新增，建议作为 U6 延伸 | 需要 5–10 个交易日数据回填 |

#### 3.6 行动项（新增，与 P1–P6 不冲突）

| # | 行动 | 依赖 | 成本 | 风险 | 预注册判据 |
|---|---|---|---|---|---|
| **P7** | `_build_clue_brief` 在 user prompt 顶部追加 `[今日输入源健康]` 块（与现有健康日志同源） | 无 | 30 分钟 | 零 | 健康头出现后，模型引用已失效/缺失源的比例下降 |
| **P8** | 将 `SYSTEM_PROMPT_V2` 从 11 条平铺规则重组为 "角色 → 任务 → 4 层纪律 → pre-mortem 自检 → 输出格式" | P2/P3/P4/P5 文本改动完成后 | 1 小时 | 极低 | rubric_score ≥3 占比从 94% 下降；invalidation/short_check 字段缺失率下降 |
| **P9** | 实现 per-item JSON schema 校验器（存在性检查 + 白名单 repair），替换当前 `schema_required={"surfaced": list}` | U4 | 半天 | 低 | repair 后字段完整率=100%；repair 未改变任何 direction/confidence/thesis |
| **P10** | critique 二选一：A) 改为主调用内 pre-mortem（把反证写入 invalidation）；B) 保留独立调用但使用完全独立的 system prompt（不与主调用共享） | U3 裁决 | 1 小时 | 中 | critique KEEP/DOWN/DROP 与 T+1 hit 的区分度转正；或仪式化攻击率从 96% 下降 |
| **P11** | 负反馈表增加错误类型归因，prompt 中展示 Top-3 高频错误模式 + 最近 3 例 | U5/U6 数据落盘 | 2 小时 | 低 | 加入归因后，同类重复错误率 10 交易日内下降（需与未加入组对照） |

#### 3.7 诚实边界（再次强调）

- **Prompt 工程不能替代信号源质量**。UE 当前全史 −$406.63 的核心问题可能不是 prompt，而是"事件驱动浅分析在美股短期价格预测上的基础信噪比低"。
- **所有建议都需预注册判据**：例如"重组 prompt 后，rubric_score 塌缩率是否下降？""错误类型归因后重复错误率是否下降？"不预注册，无法区分优化效果与随机波动。
- **不改 live 执行行为**：本轮建议除 U2/U4/U7 等已列代码项外，其余均为 prompt 文本/结构/测量层变更。


### 第 4 轮 — 独立复核 + 第一批落地 (2026-08-25 夜, 用户批"需要")

**独立复核 (Hermes, 不信任文档自述, 逐条对码对库重算)**:
- 全部关键数字复算吻合: surfaced 83/42/27/14、tier 47/21/1、rubric≥3 94.2%、critique 42/24/3、
  conf 列 50×119+6 早期值、invalidation/conditions 125/125 空、SHORT 27→9、执行账本
  −$406.63 (34笔−437.34 / 18笔+30.71, WR 26/52) 按 decision_id 精确 join 逐分吻合、
  tier eval 双档 IS/OOS 反号、病灶1/2/3/4/5/6/8 逐条代码锚点确认。
- 两处措辞校正: ①streak 链最长为 TSM streak=8 (非"以XOM为主", XOM 3-6); ②conf 分布上界
  实为 0.92 (一只) 非 0.85。均为小误, 不动结论。
- **复核发现文档遗漏的现行病灶 (今晚实弹抓到)**: push 文案说谎 — 8/25 21:10 cron 8 只浮现,
  7 只无现价 + 1 只 DA 排重, 实际写库 0 条, 但推送仍打"已注入DB→执行轨道"。
  根因: mode 字符串只看 entry_enabled 开关, 不看实际落库数。比病灶6更重一层: 对外宣称与实际相反。
- **病灶8状态校正**: 配置缺口属实 (无 max_emergence_streak 键/无过滤行/确认窗口已过),
  但 8/25 浮现 8 只 streak 最大=2, 今日实际漏闸损害=0; 伤害是未来的连涨链, 不是今天。

**第一批落地 (用户批, 零行为风险)**:
1. **U1**: PROMPT_VERSION v2.4→v2.5 (对齐 8/16 既有行为改动) + 新增 `PROMPT_HASH_V2`
   (sha1[:12], 实测 `3c501d513c47`) 落库三处: scan jsonl / raw_instruction / prompt_eval。
2. **U2**: cmd_inject instruction dict 补顶层 `"invalidation"` 键 → save_instruction 自动写
   invalidation 列 → tripwire_checker 文本轨即刻覆盖 UE (r15 接线的对称延伸)。
   诚实预期照抄文档: 现有 invalidation 多非数字子句, 首期命中率≈0, 价值=审计覆盖。
3. **P2**: rule 4 "多空对等" → "多空机会对等浮现, 质量门槛各自对称" (零行为, 消除与
   rule 8/10/11 单侧约束的字面冲突)。
4. **P3**: rule 11 删括号内月度实证引用 "(回调日 LONG 净正 / 追涨日 LONG 净负)", 保留规则本体。
5. **新增 (复核发现)**: push 文案改查当日真实落库行数 — 实测今晚干跑输出
   "注入0条 (候选8只被现价缺失/排重/冷却等过滤, 见universe_scan.log)"。

**验证**: py_compile 过; push --dry 实跑 (今晚数据) 文案修复生效; PROMPT_HASH_V2 可导入读出;
db.py:501 顶层 invalidation 键读取确认 (save_instruction 无需改动)。

**新发现待跟进 (本轮未处理)**: 复核期间实测 `_batch_quotes` 挂起, 日志 `[FutuLV3] 批量快照失败:
ret=-1`, OpenD 进程在、端口 11111 在监听但快照请求挂死 → 今晚 8 只里 7 只"无现价"的直接根因
即行情源故障 (非代码问题)。这是独立运维事项, 另案跟进; 同时也解释了为何今晚无现价过滤吃掉 7/8 候选。

**预注册判据更新**: U2 计时起点 = 2026-08-26 (首个带顶层 invalidation 的注入日)。

### 第 5 轮 — 8/25 零注入根因定案 + 修复 (2026-08-25 22:30)

**撤回**: 第 4 轮曾诊断"行情源故障"——错误。实弹复测证明 OpenD 一直健康, 根因是代码层毒批污染:

**证据链 (逐只实测)**:
1. 逐只快照: `US.NUMD`/`US.LGMK` → ret=-1 **"暂不提供美股 OTC 市场行情"**; 其余 6 只主板票 → ret=0。
2. Futu `get_market_snapshot` 语义: **批内任一标的不支持 → 整批拒绝** (不做部分成功)。
3. `futu_lv3_provider.get_batch_quote` 旧实现: `ret!=0` 时直接返回空缓存, **不落入逐只降级** (降级只在 except 路径)。
4. 后果: 2 只 OTC 票毒死整批 → 主板票拿不到 LV3 `pre_price` → 盘前锚定门 (`_effective_quote_price`) 全判"无现价" → 0 条注入。
5. 盘前锚定门 (8/11 防锚错修复) 在盘前时段要求 `pre_price>0`, 否则宁缺勿滥——这是对的, 它暴露了上游毒批。

**修复 (两处, 均为加法式)**:
- `get_batch_quote`: `ret!=0` 不再提前返回, 落入既有逐只降级循环 (与 except 路径同语义), 毒票只杀自己。
- `get_quote` 单只快照路径: 补 `pre_price`/`pre_change_rate` 字段 (与批量路径 8/11 修复同源同口径)——此前漏补, 降级后盘前锚定门仍拿不到盘前价。

**验证 (实跑, 8/25 21:10 同款毒批复现)**: 修复后 6 只主板票全部存活且带 pre_price; 2 只 OTC 单独缺失; 端到端 `_effective_quote_price` 8/8 全部可注入。

**边界裁决**: OTC 票 (NUMD/LGMK) 是否进池另案——本次只修"毒死主板票"; 它们自身仍会被盘前锚定门正常过滤, 且未来可加池子准入排除 (不在本批行为风险预算内)。

### 第 6 轮 — U7 streak 硬闸实施 (2026-08-25 夜, 用户批"其他决策同意")

按 §八.6 修正清单全五步落地:
1. **FTSeek 消费端**: `cmd_inject` 候选过滤加 `_streak_passes(s, _max_streak)` (谓词独立可测,
   默认99缺键零行为)。
2. **skill 消费端**: `_load_ue_short_candidates` SHORT 循环加同规则过滤 (读同一 yaml 键);
   `ue_streak_days` 透传 8/14 §12 已就位, 只补过滤本身。
3. **配置**: `max_emergence_streak: 2` (≤2放行, ≥3拦; 回滚=99)。
4. **测试**: `tests/test_ue_streak_gate.py` 10 例 (四态 None/0/1/2/3/8 + 缺键默认全放行) 全过。
5. **落档**: 提案 status→已实施; 首个生效窗口 8/26 21:10 (晚于今日窗口, 不抢)。

口径勘误留痕: 提案 §八.6-4 写"配置缺键默认不放行", 与 §二 落地描述 (缺键99=全放行) 矛盾;
按已裁决的 §二/第213行语义执行 (缺键=99=全放行, 防配置未读到静默启闸)。

### 第 7 轮 — U3 critique 结案: 删除路径执行 (2026-08-25 夜)

**全口径实算 (182 条 T+0 join / 157 条 T+1 / 52 笔实际账本, 全部对库复算)**:

| 口径 | KEEP | DOWNGRADE | DROP | 结论 |
|---|---|---|---|---|
| T+0 锁定 | +5.14% (n=13) | +1.96% (n=70) | +0.66% (n=37) | 正向区分, 置换检验 p=0.02 |
| T+1 执行 | −0.70% (n=14) | +0.87% (n=68) | −1.24%* (n=35) | **与 T+0 反号**, p=0.82/0.12 不显著 |
| 实际账本 | +$54 (n=2) | **−$286 (n=19, 最大亏损桶)** | **+$85 (n=10)** | DROP 桶实际盈利 |

*剔除 2 只肥尾 (SLE −25%/AMCI −26%, 均 < $5 仙股) 后 DROP 转正 +0.23%。

**判据适用**: 审计 §判据表锁定 "T+1 倒挂持续 (DOWN/DROP 命中 59.2% > KEEP 42.9%, n=103≥30) → 删除"。
T+0 显著性不推翻判据 (判据口径 = T+1 执行口径)。且实际账本证伪 "DROP=无价值" 假设。

**执行 (预注册删除路径, 双端一致)**:
1. `llm_universe_scan.py`: v2.2 self-critique 块整体删除 (省 1 次 LLM/日); 恢复锚点 2fd79ab3。
2. skill `pipeline.py`: DA feed DROP filter 移除 (borrow_feasible 过滤保留); 恢复锚点 c82adc6。
3. `live_readiness_report.py` G1-3 门: 裁决退役 (pass=True + 留痕) — 判据按删除路径达成,
   历史倒挂读数仍显示供审计。RFC-062 文本修订需另行 DEB+用户裁决 (8/29 门 1 会读到退役标注)。
4. 历史数据零影响: 快照/scan jsonl 历史行的 verdict 保留, 仅新样本起为 None。

**下游悬空核查**: 快照 factors.verdict → None (自然降级); skill ue_critique → 空串
(条件注入跳过, DA prompt 零污染); scorecard 历史桶冻结在删除前读数。
