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
        "conclusion": "⚠️ 反直觉: 榜内横截面 IC=−0.44 / t=−13（榜首之后**续跌**）, 但实盘里买 top10 的 11 笔反而 +$15.20/笔 ⇒ 榜级与笔级**符号相反**、不可互相代入。用途未定 (排除信号? 反向候选池? 并入既有追高族?) 三条路都要另立预注册",
        "cls": "NEEDS_USER",
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
        "conclusion": "多头被拦 n=36 均值 −1.9 但中位 +1.1（尾部驱动）; 空头被拦 n=16<30 ⇒ 不裁决, 延长观察",
        "cls": "AUTO_WATCH",
    },
    "penetration_shadow": {
        "plain": "价格穿透关键位之后的行为（假设集由 LLM 生成）",
        "conclusion": "基线穿透率 97.4%; 假设集 None（本次未调 LLM）⇒ 不判",
        "cls": "AUTO_WATCH",
    },
    "premarket_invalidation": {
        "plain": "盘前证伪条件 A/B/C（跳空/方向失效就作废这单）",
        "conclusion": "A/C 已按 2026-09-11 处置为『不转正』; B 执行样本 n=4<20 ⇒ 攒样中。⚠️ 但实测发现相位不符: 产物 396 条全在开盘后 5–17 分钟 (用盘中价), 判据文字说的是『盘前』⇒ 裁决前须先定谳口径",
        "cls": "AUTO_WATCH",
    },
    "reentry_quick_tp_gate": {
        "plain": "再入场的『快止盈』被门挡住, 到底亏不亏",
        "conclusion": "n=13（门 10）刚够; 门禁与快收参数在 9/1–9/30 窗口内**禁改**, 到期一次性结算",
        "cls": "WINDOW",
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
    }
}
