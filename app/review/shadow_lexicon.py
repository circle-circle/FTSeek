"""影子/探测器「白话词典」—— 判决书与影子验收清单共用的单一真源 (SSOT)。

为什么放在 app/ 而不是 scripts/: 消费方有 app 层 (app/review/bundle_render.py 的推送白话注释)
与 scripts 层 (scripts/pending_adjudication_report.py 的影子验收清单)。scripts → app 是正常方向,
反过来会让 app 依赖 scripts。两处各写一份白话 = 同一个影子在两个推送里说法不一致 (用户会当成两个东西)。

字段:
    plain       ── 「这是什么 / 在问什么」一句话白话 (去术语)
    conclusion  ── 「现在什么读数 / 结论」快照 (来自已实测读数; 变化时判据可能翻盘, 故是快照不是永久真值)
    cls         ── 处置分类: NEEDS_USER(要用户裁) / AUTO_WATCH(自动续观) / WINDOW(等窗到期结算)

出处: docs/decisions/RFC-092-*.md 三批实测 + 2026-09-21 本 agent 重跑复现
(top_movers / iv_hv / gap_chase / top_movers_trade_join)。**未收录的影子不得编白话** ——
消费方一律回落到该影子的注册判据原文 (bundle 里有 `criteria`) 并标注「未收录白话名」。
"""
from __future__ import annotations

SHADOW_LEXICON: dict[str, dict[str, str]] = {
    "futu_channel_top_movers": {
        "plain": "涨幅榜前 100（追高族）: 榜上标的之后到底是续涨还是回落",
        "conclusion": "**已结案（SUPERSEDED, 2026-10-03 终裁）**：用途三岔路已裁「③并入 KR3 方向×涨幅轴, 本影子不转正/不加门」; 而 KR3 已于 2026-10-15 两腿全证伪 KILLED ⇒ 本轴整体关闭。榜内横截面 IC=−0.44/t=−13（榜首续跌）与笔级 +$15.20/笔符号相反的历史读数留档备查, 不构成新轴",
        "cls": "AUTO_WATCH",
    },
    "futu_channel_iv_hv": {
        "plain": "财报前 IV/HV 高低能不能预告『这单摸不摸得到肉』",
        "conclusion": "三个波动桶都没测出额外效应（medium 差 +$35.7 但日聚类 t=2.03 / MDE±52 不可分辨; high 无效应）⇒ 无残存效应",
        "cls": "AUTO_WATCH",
    },
    "futu_channel_ofi_baseline": {
        "plain": "订单流失衡基线（给资金流做对照用的那把尺子）",
        "conclusion": "只测到『资金流相对它无增量』(−0.12); 它**自身**的预测力尚未单独测 ⇒ 不构成停采依据",
        "cls": "AUTO_WATCH",
    },
    "gap_chase_gate": {
        "plain": "跳空 >5% 还追进去, 拦掉这笔划不划算",
        "conclusion": "被拦 n=31 笔均 −$16.6 ✓、样本 ✓, 但单侧符号检验 p=0.93 ✗ ⇒ 3 项过 2, 继续攒样（2026-09-21 重跑复现）",
        "cls": "AUTO_WATCH",
    },
    "entry_delay_confirm": {
        "plain": "等回调确认再入场, 是不是比直接追进去好",
        "conclusion": "全样本看着过线, 但前瞻子样本全不过 + 按日聚类 MDE±7.95 ≫ 效应 ⇒ 不可分辨, 不呈裁",
        "cls": "AUTO_WATCH",
    },
    "exit_disable_cf": {
        "plain": "某些退出理由如果关掉, 结果会变好还是变差",
        "conclusion": "每条规则的样本 n<50（门 50）⇒ 未到门槛",
        "cls": "AUTO_WATCH",
    },
    "extreme_gap_ban_shadow": {
        "plain": "极端跳空当日禁止开仓, 拦掉的是不是真该拦",
        "conclusion": "被拦组净 −$1,826 ✓、样本 ✓, 但**误杀率 53% > 40% ✗** ⇒ 判据未过, 维持纯观测",
        "cls": "AUTO_WATCH",
    },
    "gap_alignment": {
        "plain": "跳空方向与趋势一致的追单, 是不是更差",
        "conclusion": "各格样本 5~22 只（门 30）⇒ 未到门槛",
        "cls": "AUTO_WATCH",
    },
    "intraday_loss_breaker_a_prime": {
        "plain": "盘中亏损熔断如果上线, 会不会挡掉本该赚的单",
        "conclusion": "被挡笔期望 **+$45**（门要求 ≤−10, 方向相反）、累计挡掉 +$9,180; 但前瞻样本仅 14<30 ⇒ 按已记录规则不下结论",
        "cls": "AUTO_WATCH",
    },
    "kr3_direction_runup_gate": {
        "plain": "已经涨很多了还做多（追高禁令）",
        "conclusion": "**已结案（2026-10-15, KILLED）** —— SHORT 腿攒满样本门当日终裁: "
                      "被拦 n=30≥30, 三要素 1/3（日聚类 t=−0.30 / 声称−21.8<MDE80 56.5 不可测 / "
                      "OOS 同向✓）⇒ 干净证伪; LONG 腿 10-04 已证伪 ⇒ 两腿全证伪 = KR3 达成 "
                      "(OKR-2026-10 明文: 确认或证伪都算达成)。采集已停采, 不立即换假设",
        "cls": "AUTO_WATCH",
    },
    "rfc104_eth_event_plan": {
        "plain": "ETH 时段的财报事件执行计划层（盘前/盘后各时段该做什么的日程表）",
        "conclusion": "**已关停（2026-10-15, KILLED/阶段0 FAIL）** —— 毕业核验: 覆盖率判据 "
                      "15 个交易日从未全过（最好 6/15, 9 日漏票）+ 计划条数带上界 44 持续被破 "
                      "（均值 47.0）⇒ 用户裁「判阶段0 FAIL 关停」: 全 8 行 cron 注释退役, "
                      "阶段1 永不立项, 复活须重新预注册。计划目录与对账台账留档不删",
        "cls": "AUTO_WATCH",
    },
    "penetration_shadow": {
        "plain": "价格穿透关键位之后的行为（假设集由 LLM 生成）",
        "conclusion": "基线穿透率 97.4%; 假设集 None（本次未调 LLM）⇒ 不判",
        "cls": "AUTO_WATCH",
    },
    "premarket_invalidation": {
        "plain": "盘前证伪条件 A/B/C（跳空/方向失效就作废这单）",
        "conclusion": "**已结题（KILLED, 2026-10-23, 采集保留）**：A/C 已按 2026-09-11 处置为『不转正』；B 判据已结题，不再提请转正。相位口径 09:35–09:59 ET 已定谳；历史行仅保留审计。",
        "cls": "AUTO_WATCH",
    },
    "reentry_quick_tp_gate": {
        "plain": "再入场的『快止盈』被门挡住, 到底亏不亏",
        "conclusion": "**已结案（KILLED, 窗满机械结算）**：9/1–9/30 冻结窗到期结算 n=13, TP 触达 **0/13** ⇒ 快止盈门在该窗从未真实触发, 门无实益 ⇒ 关停。归档留底",
        "cls": "AUTO_WATCH",
    },
    "vwap_gate_ab": {
        "plain": "VWAP 相位门拦下的单, 是拦对了还是误杀",
        "conclusion": "补齐『按日聚类 MDE』后: 被拦组 +0.176% 但 MDE±0.39（是效应的 2.2 倍）⇒ **不可分辨**, 不改门; 原『倾向拦错』读数**作废**",
        "cls": "AUTO_WATCH",
    },
    "da_blocked_cf": {
        "plain": "被 DA 门拦下的方向性信号, 事后到底涨还是跌（拦得对不对）",
        "conclusion": "T+1 −0.051% 未越过 ±0.15% 判断线 ⇒ 继续攒样（T+5 −0.79% 仅一致性披露, 不参与裁决）",
        "cls": "AUTO_WATCH",
    },
    # ── 2026-09-26 RFC-110 补登记批 (三条影子判据此前在注册表查无 ⇒ 一切自动化不可见) ──
    # conclusion 均为**登记当日实跑**读数, 不是转述判据文 (词典规则: 须来自实测)。
    "eod_depth_gate": {
        "plain": "亏得比较深的仓（EOD 时点水下超 2.5%）到底该不该给『扛过夜』的豁免",
        "conclusion": "**已终裁（2026-10-14, GATE_NOT_WORTH 终态）** —— 结算器到达机械判负: "
                      "n=9(≥8)/ΣΔ=−293.98(≤0)/误杀 5(>1); 误杀行永久冻结只增不减 ⇒ 判负不可逆, "
                      "转硬闸路径正式关闭（门非失灵, 是 −2.5% 深度区分度不足）。处置=record-only 续采, "
                      "停采另裁 (DEB-20261009 §42, 用户裁「登记」)",
        "cls": "AUTO_WATCH",
    },
    "eth_exit_forgone_upside": {
        "plain": "盘前/盘后一到保本线就出, 是不是卖早了（后面还有没有肉）",
        "conclusion": "⚠️ 结论已出但**口径待你裁**: 按判据字面 n=276（门 20 ✓）, 但出场后当日收盘均值 −0.2249%（要 >+0.085% ✗）、只有 18.2% 为正（要 ≥60% ✗）⇒ **没卖早, 不需要宽限窗, 维持『触线即出』**。问题在 n 轴虚高: 276 笔里 97.5% 是 tighten（收紧止损参数, 不是真出场）; 剔掉后真出场只 n=8（均值 −13.12%、正值率 12.5%, 同样未过线但样本不足）。两种口径**都指向不提案**, 所以结论稳; 但改判据集合=重裁, 须你裁 (A) 维持字面集合 / (B) 剔 tighten 重攒 n≥20",
        "cls": "NEEDS_USER",
    },
    "eth_exit_quote_age": {
        "plain": "盘前/盘后出场时报价是不是太旧了, 旧报价有没有让成交价变差",
        "conclusion": "带报价龄的执行行 n=6（门 20, 不够）, 且龄 >120 秒的执行行 = **0**（6 笔全 ≤30 秒, MU 只有 2.2 秒）⇒ 落在判据分支②方向（维持现状、不加龄门）。诚实预期: 十天只攒到 6 行, 到 n=20 约需一月; 而 >120 秒迄今 100% 出现在**本来就不执行**的 hold_no_stop 行上 ⇒ 本项可能长期停在『留档反证』而不是攒到可裁, 这是判据文写明的预期结局之一, 不是断供",
        "cls": "AUTO_WATCH",
    },
    # ── 2026-09-26 RFC-109② 补登的看板产物 (原绕过注册表 ⇒ 白话词典也漏收, 推送会印
    #    「未收录白话名」) — conclusion 只用**已实测读数**; 未跑过 eval 的如实写「未预注册/
    #    攒样中」, 不编数字 (词典纪律: 未收录不得编白话, 已收录更不得编读数) ──
    "sltp_verify_shadow": {
        "plain": "LLM 给的止盈止损位和代码算出来的不一致时, 到底该信谁",
        "conclusion": "已攒 4233 行（看板第一段, 分歧率/中位分歧逐日印）。**判据未预注册** — 生产者只自述目的、没有数值门 ⇒ 现在**不能裁决**; 要裁须先立 DEB 定门（如「分歧>X% 时按实盘结局判哪侧更准」）",
        "cls": "AUTO_WATCH",
    },
    "exec_gate_audit": {
        "plain": "所有下单前的闸门（edge/飞刀/费用…）逐笔留痕: 每笔是被放行、拦掉还是延后",
        "conclusion": "3991 行审计留痕（看板印 action 六类 + edge_verdict PASS/BLOCK）。**永久测量项, 无毕业门** — edge 门本体已裁 DEPRECATED-SHADOW（DEB-20260723 C-1: 唯一特征 confidence 实证反指, 永不激活为 live gate）⇒ 本产物只做漏斗审计与 per-trade 归因, 不参与转正",
        "cls": "AUTO_WATCH",
    },
    "exit_param_shadow": {
        "plain": "把保本/回撤退出调得更紧一点, 是不是能少亏（同批笔对照算『若早出会怎样』）",
        "conclusion": "9/26 实跑毕业三门（N≥30 / 均值改善 / 一致性≥55%, N=495）: 更紧保本族 5 个变体**全 FAIL**（最好的 be0.3_dd0.3 均值 +0.191 但一致性 38.2%<55%）、30min 提前离场 FAIL（−0.0299%）、早盘紧 SL 族 5 变体全 FAIL（一致性 45.9–47.5%）⇒ **无新变体可毕业, 维持现行退出参数**。唯二 PASS 的两个变体对应的是**已毕业的生产退出腿**（dead_signal 60min, 8/10–9/21 实跑 36 笔）= 既成事实确认, 不是新提案",
        "cls": "AUTO_WATCH",
    },
    "universe_expansion_shadow": {
        "plain": "板块轮动时去『选股宇宙之外』找异动票, 这条扩池路值不值得真开",
        "conclusion": "已攒 49 行。**判据未预注册**（生产者自述「验证有数据后再决定是否驱动扩池」, 无数值门）⇒ 现在不能裁决; 要裁须先立 DEB 定门。注: 扩池会**真改变候选池**, 属 L3, 必须先有预注册判据",
        "cls": "AUTO_WATCH",
    },
    "early_dead_signal_shadow": {
        "plain": "开仓后 15/30 分钟还没摸到肉的单, 是不是『死单』——提前砍掉划不划算",
        "conclusion": "**已退役（2026-10-31 用户裁「退役不转正」, status=KILLED）** —— 全样本复算 `early_dead_signal_report.py`: 反事实净额 **−$1,641（30min）/ −$1,498（15min）**（9/26 读数为 −$595/−$415, 样本翻倍后更差）; 命中组「亏→盈（早退砍反弹）」占 **63%**, 「亏→亏（早退少亏）」只 29% ⇒ 检查点早退必连反弹单一起砍。判据①(55% 绝对线) 在现役词表下**结构性不可达**（预注册词表基率 0.8% vs 当时 30%）, 相对意图 3.74× 虽过但**与能否接线无关**。采集保留作轻量回归监测, 不再提请转正; 复活须新 RFC + 新预注册判据（复用本判据 = 阈值购物）",
        "cls": "AUTO_WATCH",
    },
    "premarket_peer_shadow": {
        "plain": "存储板块几只票（MU/SNDK/WDC/STX）盘前同向+放量, 能不能当彼此的佐证信号",
        "conclusion": "已攒 56 行。**判据未预注册**（生产者只列假设问句, 无数值门）⇒ 现在不能裁决; 要裁须先立 DEB 定门。生产者自述打法同 divergence_arbitration: 先攒证据, 有 edge 再谈切执行前置闸门",
        "cls": "AUTO_WATCH",
    },
    "short_funnel_shadow": {
        "plain": "空头这条链的漏斗: LLM 分析出多少只看空 → 真出了几条空头指令 → 执行了几笔, 断点在哪一层",
        "conclusion": "10/31 用户裁「**不立门**, 先回测」, 并已建 `scripts/short_blocked_cf_eval.py` 出读数。10/31 复跑漏斗: 49 交易日; DA 分析 1025 只 → 看空 149（14.5%）→ 空头指令 88（转化 59.1%）→ 成交 52; 被掐 61 只的原因 = `bridge/通道候选未提名` 26 + **`entry_confirm_reversed` 22（全部 10/12 门上线后新增的掐口）** + PRICE_TOO_LOW 5 + 其他 8。反事实（SHORT @ 当日开盘, T+1, 费用线沿用 B14 ±0.15%）: bridge 桶 n=25 均 **+1.256%**（p=0.115）; 全人群 n=47 均 **+0.990%**（p=0.191, WR57.4%）⇒ 方向偏正但**统计不显著**, 且本口径无 SL/TP（实际退出会削尾部）⇒ **不构成开门依据**。下一步 = 用现役退出规则重跑 + 每桶攒到 ≥30。参考: 10 月空头 43 笔 +$484（+11.26/笔）vs 多头 100 笔 −$238（−2.38/笔）⇒ 空头确是当下赚钱的一侧",
        "cls": "AUTO_WATCH",
    },
    "ue_short_boost_shadow": {
        "plain": "UE 扫出来的空头指令, 如果给它 ×1.25 加码, 会不会比不加码更赚",
        "conclusion": "**已退役（2026-10-15, KILLED）** —— 写方链 10-06 随 UE fresh-event 改造退役, 产物冻结 54/60 主门永不可达; 用户裁「冻结样本结算后归档」: 一次性结算 52/54 可结算, T+1 方向调整均值 +0.05%（t=0.667, MDE80=2.94pp ⇒ 即使到门也无可测 edge）⇒ 归档结案, 不再攒样",
        "cls": "AUTO_WATCH",
    },
    "overnight_earnings_carry": {
        "plain": "财报当天的 EXT 候选票, 能不能扛过夜吃隔夜 carry（原判据问『同日』语义该不该放宽）",
        "conclusion": "**已退役（2026-09-24, KILLED）** —— 判据（n≥3 交易日 ∧ 过闸上界≥2 只/日）未达, 触发条件在生产中不存在 ⇒ 按判据明文停采。产物与登记留档不删",
        "cls": "AUTO_WATCH",
    },
    "edge_gate_shadow": {
        "plain": "那道已废弃的 edge 门（唯一特征是 confidence）留下的对照基线, 用来做逐笔归因",
        "conclusion": "302 行对照记录。**无毕业门**（DEB-20260723 C-1 逐字: 本门**永不激活**为 live gate —— confidence 已实证反指 IC 为负, 桶级期望 ±0.001~0.009 是噪声级、每日随 latest.json 翻转）⇒ 只作 per-trade 归因的对照基线, 不参与任何转正。任何激活提案须先过 INTC 测试 + 独立 DEB",
        "cls": "AUTO_WATCH",
    },
    "meta_label_shadow": {
        "plain": "不猜方向, 只猜「这条已有信号该给多大仓位」——把差单压小而不是砍掉, 交易量就能保住",
        "conclusion": "**首版已证伪 (2026-10-31, status=KILLED)** —— 用 6 个已落库的事前字段 (方向/时段/ATR%/VIX/价位/条件数) 训练: OOS AUC **0.536** (purged 日块) / **0.394** (时间切分, 反指); 分三等份 **top −$10.11/笔 vs bottom −$5.92/笔**(单调性反向) ⇒ 排序力判据 FAIL。三档仓位政策净额 −$1,072 vs 基线 −$1,315, 但**笔均仍是 −$1.80** ⇒ 只是少下注, 不是选样。⚠️ 被否的是「这套宏观特征」, 不是方法: 复活 = 换 11/02 起采的 OBI/微价特征重跑同一套 G1/G2/G3",
        "cls": "AUTO_WATCH",
    },
    "fee_gate_shadow": {
        "plain": "按『费用占比 / 名义额』立一道费用门, 把费用吃掉利润的单挡在外面, 划不划算",
        "conclusion": "10/31 实跑 `fee_gate_shadow.py`: 基线 n=207 净 **−1.21/笔**（WR78.7%, 费 9.33bps）—— 高胜率+负期望 = 少数大亏主导。各候选门 **blocked 组反而更赚**（notional<5000: 被拦 +7.59/笔 vs 留下 −3.12/笔; <4000: +3.71 vs −1.48）⇒ **按这些轴立门会砍掉赚钱的单**, 无可立项门。下次重看 2027-01-31",
        "cls": "AUTO_WATCH",
    },
    "overnight_exposure_rules": {
        "plain": "隔夜仓要不要砍一半 / 干脆不留过夜 / 只让浮盈的仓过夜",
        "conclusion": "**KILLED（三条全证伪，勿再提）**。跨日 75 笔拆两腿: 日内腿(入→收盘) **−$5,396**、隔夜腿(收盘→次日) **+$2,137** ⇒ 过夜是**减损**不是亏损源。禁隔夜 −$5,396（Δ−2,137）/ 减半 −$4,477（Δ−1,218）/ 只留浮盈 −$5,174（Δ−1,915），一致性 29–35% 全 <55%。**根因**: 12 笔跳空单收盘时浮亏 −0.7~−2.3%，而 44 笔赚钱跨日单收盘时也是 −1.2~−2.3% ⇒ 收盘时点不可分。复活前置: 市况转单边 + n≥200 且跳空≥30 笔",
        "cls": "AUTO_WATCH",
    },
    "confidence_tier_gate": {
        "plain": "按「这条指令有多确定」(confidence) 分两档: 低档那 408 笔到底是不是纯送手续费",
        "conclusion": "**2026-10-31 读数: KEEP_SHADOW (不许当门用)**。conf<60 的 408 笔毛利/R = **−0.001 ≈ 0**, 净 −$2,261 恰好 ≈ 408×$5.5 费用 ⇒ 结构上「零边际人群被费用抽干」; conf≥60 的 282 笔毛利/R +0.037, 净 **+$1,014**。两个子期 (7-8月 / 9-10月) 同号 (差 +0.033 / +0.047), 但 **Welch t 仅 1.29** ⇒ 不显著。转正条件: t≥2.0 且零边际结构成立 (T1∧T2∧T3) ⇒ 才可另立 DEB 上硬门",
        "cls": "AUTO_WATCH",
    },
    "budget_p3_sizing": {
        "plain": "三道预算帽: 全仓同时止损最多亏多少(2.5%) / 太小的单不做(名义≥$5,500) / 止损太宽的单不做(>8%)",
        "conclusion": "**2026-10-31 用户裁「P3上」已生效**。①组合在险总帽 2.5%=$2,490 —— 实测并发峰值 18 笔=5.1% 权益同时裸在止损上, 这是原来完全没有的一层; ②名义下限 $2,500→$5,500 (费用 15.9→7.2bps; ⚠️ 其效力≈「高价股不做」, 非费用因果)**同日修正: 热帽 2.5%→4.0%** (实测 2.5% 砍掉 87 笔盈利单 +$785; 笔数 556→642); ③止损上限 8% (715 笔 p90 5.43%/max 8.5% ⇒ 只拦 3 笔, 不伤 SHORT 恒 6.09%)。回放: 556/715 笔保留 (剔 69 名义+87 热帽+3 止损), 净 −$190→**+$1,117**。⚠️ 但同批不同帽组合符号会翻 (−660/+1,117), 经济效应在噪声内; 确定的是笔数↓、费用率↓、最坏同时亏损 5.1%→2.5%。回滚判据见结算器",
        "cls": "AUTO_WATCH",
    },
    "risk_cap_sizing": {
        "plain": "给每一笔定一个『最多亏多少美元』的硬上限（$80），止损越宽就买越少股 —— 不改选股, 只改下注大小",
        "conclusion": "⛔ **上线当日即证伪并回退**（2026-10-31, status=KILLED, config 已回 0）。病因真实（每笔风险中位 $257 vs 中位盈利 $4.29），但**缩小仓位这个解法反了**：佣金地板 `max(min($0.01×股数,0.3%×额), $1.99)/腿` 让 91.9% 的单恒付固定费，帽把名义额压到 $1,546 后 **费用率 7.3→26.9bps，净额 −$225 → −$1,909**。全样本毛利 **7.83bps < 费用 11.60bps**，盈亏平衡名义额 ≈**$8,964/笔**（当前 $4,849）⇒ 真正要动的是费用线（佣金档位 $1.99→$0.99 即可转正）或止损宽度（≤3.2% 才能让风险帽与名义额≥$2k 相容），不是把仓位变小",
        "cls": "AUTO_WATCH",
    },
    "ue_confirm_gate": {
        "plain": "UE 扩池出来的候选, 等『价已进来 + 再确认』两段都成立才 inject, 是不是更划算",
        "conclusion": "10/31 产物复现 **KEEP_SAMPLING**: PRICE_IN 桶前瞻 **+3.99%**（判据要 ≤0）、CONFIRM 桶 **−0.01%**（判据要 >0）⇒ 两个符号都和假设相反, 但 n=98/92、日聚 t 未达 ⇒ 继续攒样, 不转正",
        "cls": "AUTO_WATCH",
    },
    # ── 2026-10-31 DEB-20261031 清账批: 5 条此前「未收录白话名」的影子补白话 ──────
    #    (推送原印「（未收录白话名）— 以注册表 description 为准」= 让用户自己翻译)
    "execution_tca_spread_shadow": {
        "plain": "我们的成交到底付了多少点差和滑点（按点差/报价年龄分桶看扣费后的净 bps）",
        "conclusion": "10/31 实跑 `execution_tca_report.py`: 成交 37 笔但**只有 8 个 ET 交易日** ⇒ 预注册样本门（≥10 日 ∧ ≥30 笔）**未过**, 注册表 37/30 只数笔数不数交易日。读数（未过门, 只作参考）: 点差 p50 13.79 / p90 29.46 bps; 报价年龄 p50 1.89s / p90 14.3s; 计划→成交 均 +111.4 / 中位 +126.8 bps（**方向语义未定谳, 不得当成本**）。下次重看 2027-01-31",
        "cls": "WINDOW",
    },
    "entry_confirm_forward": {
        "plain": "触发后等 2 分钟再进场（价格还站在触发方向才进）, 到底救不救得回『一进场就反向』那族亏损",
        "conclusion": "**已重开（2026-11-05 用户裁「重开 entry_confirm v2 形态」, status=OBSERVING）** —— 执行: `entry_confirm_enabled: true` + holdout_pct 0→**20** (10-31 关门裁决写明的复活血路: 无对照臂永远不可归因) + 同模块 RFC-153 FADE_SHORT 追价帽**显式关** (一次一变量, 其判据从未被裁决)。形态 v2 不变 (mid-vs-mid + grace 2-of-3, delay 120s = 影子唯一裁过的腿); **新判据走 RFC-158 预注册** (RFC-141 §4 已被 10-31 INSUFFICIENT 消耗)。⚠️ 上一段(10-16~10-30)判决 FAIL **不是机制证伪**: 全体净均 −$0.80→+$3.78、净和 −$352.67→+$332.85、SL_INITIAL 族 −$162.34→−$127.08、非族 +$9.74→+$15.09、避免率 42.9%、opp_cost 中位 0.208% —— 卡在「族净均绝对严线 −$40」(n=7 时不可达) + opp_cost 均值 0.313 vs 0.3 (差 4bps) + 开盘窗子集变差。**持续披露三块已知副作用**: ①通过率约 51% ⇒ 门砍掉约一半触发; ②空头通过率 42.1% vs 多头 56% ⇒ 空头被砍更狠, 而空头是 10 月唯一赚钱侧; ③上一段「误杀 0」系事后族标签口径的幸存者偏差 (live 前瞻里被掐的 22 个空头方向偏正, n=47 均 +0.990%)。读数: cron 37 6 * * 2-6 settler 每日出 entry_confirm_forward_latest.json",
        "cls": "AUTO_WATCH",
    },
    "instruction_conflict_shadow": {
        "plain": "同一个 decision_id 被写了两次、方向还互相矛盾 —— 这种冲突多久发生一次, 仲裁有没有漏",
        "conclusion": "10/31 实跑 jsonl: **90 次冲突 / 19 个交易日**（2026-10-01..10-28）, 最终表内同 decision_id 多行 **0**（= 仲裁都收敛了）, 全部来自 DA（BUY 54 / SKIP 25 / SELL_SHORT 11）⇒ 判据①② 过。判据③「checkpoint 扫描 = 落盘」**还没有结算器** ⇒ 转正前须先补 eval, 本项不算全过",
        "cls": "AUTO_WATCH",
    },
    "blocked_exit_opportunity": {
        "plain": "想出但被『冻结/证据待补』卡住的出场单, 卡住的那段时间实际亏了多少",
        "conclusion": "10/31 实跑: **48 个 episode**, 冻结时长中位与 p90 **都是 0 秒**（最长 66.5s）, 期间最大不利位移中位/p90 = 0, **真的跌破止损的只有 1/48 = 2.1%**（US.CRM 10/30 单例）⇒ 冻结代价极小, **没有『告警升级/受控重评』门的立项依据**, 维持纯观测",
        "cls": "AUTO_WATCH",
    },
    "factor_neg_edge_shadow": {
        "plain": "打上『负 edge 因子』标记的单, 事后是不是真的更差（该拦）还是反而更好（拦错了）",
        "conclusion": "10/31 实跑（全史 701 行）: **多头侧有判别力** —— 被标 SKIP n=373 净 **−8.66/笔**（WR57%）vs 放行 −4.15/笔（WR70%）; 🔴 **空头侧方向反了** —— 被标 SKIP n=91 净 **+19.62/笔**（WR63%）高于放行 +5.17/笔 ⇒ 该因子在空头侧标记的是**更好的单**。改用法/停用 = 生产变更须另立 DEB, 本批只登记读数",
        "cls": "AUTO_WATCH",
    },
    # ── ④ 批已实跑出读数但词典漏收的 5 条 (推送原印「请跑 XXX 取读数」= 让用户重做
    #    我已做过的事) — conclusion 逐条摘自 ADJUDICATED note (实测值, 非转述) ──
    "futu_channel_capital_flow": {
        "plain": "盘中大单资金流方向（分钟级采集）对次日收益有没有额外预测力（相对已有的 OFI 基线）",
        "conclusion": "10/31 复算 `futu_channel_shadow_eval.py`（新样本 n_pairs=127, OFI 基线现有 51911 行）: 边际 rank IC **+0.0071**（t=0.079）不显著, 相对 OFI 增量 **−0.0248**, OOS 双窗 **异号**（−0.0708/+0.0547）⇒ 毕业判据（双窗 OOS≥40% 或 ρ≥0.15）**不过**, verdict=FAIL。⚠️ 9/26 那组数值（IC −0.1447 / 增量 −0.1326 / OOS 双负）**已作废**, 结论方向不变（无预测力）。**采集维持不停**（9/21 你已裁 + 分钟级原料在喂生产盘中资金背离门 `intraday_reentry`）⇒ 要动的是判据/门的方向, 不是采集; 下次重看 2027-01-31",
        "cls": "AUTO_WATCH",
    },
    "eth_earnings_direction_path": {
        "plain": "财报后价格往哪走（方向路径）能不能预告盘前/盘后单子的结局",
        "conclusion": "10/31 复算（`earnings_direction_path_eval.py`, 尺子已由 RFC-109④-D 修好 ⇒ 旧结论「结构性不可重跑」**作废**）: SHORT n=199 净 **−1.87/笔**（WR41%）、LONG n=151 净 **−0.10/笔**（WR52%）⇒ 预注册判据（每方向 ≥30 笔且净/笔>0）**不过**, 维持不转正。⚠️ 注册表 985/30 数的是 jsonl 总行数, 判据轴是**每方向笔数** ⇒ READY 是机械提升不代表达标",
        "cls": "AUTO_WATCH",
    },
    "eth_short_probe": {
        "plain": "盘前/盘后真下一张 1 股 SIM 空单, 探一探券商通道到底能不能开空（额度/借券/时段限制）",
        "conclusion": "**已退役（KILLED, 2026-10-18）**：探针目标已达成；cron 已于 10-06 删除、产物 9/26 冻结；数据保留，重开须新预注册。",
        "cls": "AUTO_WATCH",
    },
    "eth_premarket_exit_shadow": {
        "plain": "盘前持仓的几种出场法（到点就出 / 拿到开盘 / 只锁盈不止损）到底哪种净额更好",
        "conclusion": "10/31 复跑真结算器 `settle_extended_hours`（新结算 180, 累计 30246）: tighten n=125 **+0.369%** ✓（唯一稳定过线）; hold n=912 **−0.696%** ✗; hold_no_stop **持仓级 n=65 +0.153%** / 行级 n=15556 +0.272%（⚠️ 行级=逐分钟采样行, 膨胀 10.7×, 同一持仓被重复计权 ⇒ 判据该按持仓轴读）; lock_in / breakeven_lock 各 n=2 样本不足 ⇒ 判据「族净额>0 且 n≥30」只有 tighten 一族稳过, 仍不构成提案",
        "cls": "AUTO_WATCH",
    },
    "eth_afterhours_exit_shadow": {
        "plain": "盘后持仓的几种出场法（拿到次日开盘 / 只锁盈不止损）哪种净额更好",
        "conclusion": "10/31 复跑: hold_to_next_open n=112 **−6.992%**（负 MFE = 全窗从未翻绿, 再证「盘后是惯性时段不是修复时段」）; hold_no_stop **持仓级 n=51 −0.003%** / 行级 n=12235 +0.060%（⚠️ 行级 10.7× 重复计权, 判据按持仓轴读）; tighten n=144 −0.060% ⇒ 判据「净额>0 且 n≥30」**仍不过**, 但 −0.003% 属贴线（差 0.003pp 即翻面）⇒ 既不可称过门, 也不可宣称显著为负; 两时段合并评估的前置（分布无系统性差异）亦不成立",
        "cls": "AUTO_WATCH",
    },
    "breakeven_aw_variants": {
        "plain": "保本线现在设在 0.4% 处, 换成 A（别的保本位）或 W（只在开盘窗武装）会不会更好",
        "conclusion": "**已结案（2026-09-26, 维持 be_0.4, 生产参数一个未动）** —— 9/26 06:41 结算器落盘判定照抄: A 变体 era=be0.4 n=108 均值 +0.030pp 但单侧符号检验 **p=0.9998**（36 wins/108）⇒ 判据三条（n≥30 ∧ p<0.05 ∧ 均值>0）不全过 ⇒ KEEP_CURRENT_0.4; W 开盘窗 n=32（be0.4 层 25 笔 均值 −0.46pp p=0.9967 胜率 24%）⇒ 反事实为负, **不加开盘窗 gating**（再证 8/19 P1-b 撤回）。注: 注册表 n 显示 0 是**终态不再刷新计数**（refresh_counts 只刷 OBSERVING/READY）, 真样本量以结算器落盘的 n=108/32 为准; 采集与结算器保留作轻量回归监测",
        "cls": "AUTO_WATCH",
    },
    "entry_band_widen": {
        "plain": "入场带按 ATR 只放宽不收窄（追价上限 8%）— 放宽后到底比不放宽多赚还是多亏",
        "conclusion": "n=36/30 达门 ✓, 安全门过（不显著劣于 flat, p_worse=0.059>0.025）✓, 但正向证据不足（mean=−$17.7 / CI95 含 0 / 符号 p=0.31）✗ ⇒ 按预注册顺延攒样, 不翻开关",
        "cls": "AUTO_WATCH",
    },
    "eth_relative_volume_gate": {
        "plain": "事件票被绝对量闸拦下后, 用『相对自己近 20 日中位量的比值』替代绝对量门 — 拦得对不对",
        "conclusion": "**已退役（KILLED, 2026-10-23, 输入断供）**：真 n 轴 would_pass_relative=0/30（53 行均为 turnover_too_low 采样，would_pass 均 None；交易日 1/15），随后上游 RFC-104 阶段0 关停/输入断供 ⇒ 判据轴数学不可达，不呈裁；历史产物留档。",
        "cls": "NEEDS_USER",
    },
}


# ── 词典↔注册表一致性守卫 (2026-10-16, 用户令「从源头修复, 推送随源头调整自动体现」) ──
# 病灶: 词典 conclusion 是手写文本, 注册表状态变了它不跟 (实证: ue_short_boost 已 KILLED
# 而词典还说「攒样 52/60 还差 8 笔到门」, 周六载体差点原样推出)。本守卫 = 源头一致性
# 检查器: 注册表进终态 (KILLED/SUPERSEDED/GRADUATED) 而词典措辞仍是活跃态 ⇒ 暴露,
# 推送面/日检直接消费本函数 ⇒ 源头调整当天展示层自动体现矛盾, 不再靠人记得改文案。
_ACTIVE_CLS = {"WINDOW", "NEEDS_USER"}     # 活跃态分类 (攒样中/待裁)
_ACTIVE_MARKERS = ("攒样中", "还差", "待裁", "重看", "攒至", "继续攒")
_TERMINAL_MARKERS = ("已退役", "已结案", "已终裁", "已关停", "KILLED", "CLOSED",
                     "GATE_NOT_WORTH", "FAIL", "结论已出")


def check_lexicon_registry_consistency() -> list[dict]:
    """逐条对照词典与注册表, 返回矛盾清单 (空=一致)。

    矛盾形态: 注册表终态 ∧ (词典 cls 活跃 ∨ conclusion 无终态标记且有活跃措辞)。
    """
    from app.playbook.shadow_registry import REGISTRY
    reg = {s.id: (s.status.value if hasattr(s.status, "value") else str(s.status))
           for s in REGISTRY}
    out = []
    for sid, entry in SHADOW_LEXICON.items():
        status = reg.get(sid)
        if status not in ("KILLED", "SUPERSEDED", "GRADUATED"):
            continue
        cls = (entry.get("cls") or "").strip()
        conclusion = entry.get("conclusion") or ""
        has_terminal_text = any(m in conclusion for m in _TERMINAL_MARKERS)
        if has_terminal_text:
            continue  # 文案诚实标了终态 ⇒ 终态+cls=NEEDS_USER 是合法的「问题仍开放」
        active_text = any(m in conclusion for m in _ACTIVE_MARKERS)
        if cls in _ACTIVE_CLS or active_text:
            out.append({"id": sid, "registry_status": status, "cls": cls,
                        "reason": "注册表终态但词典仍活跃措辞"})
    return out
