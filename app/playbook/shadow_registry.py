"""
影子观测注册表 (Shadow Registry) — 全部 shadow/record-only 事项的单一真源
=======================================================================
解决的问题:
  本系统 shadow 事项已达 20+ 个, 分散在 config yaml 开关 + 独立 cron 脚本 +
  pipeline 内嵌落盘三种形态, 没有统一清单 → 无人知道:
    · 当前有多少影子在跑
    · 各自攒了多少样本、离毕业门禁还有多远
    · 哪些已经够样本该裁决了但没人裁
    · 哪些已废弃但 cron 还在白跑

本注册表是全部影子事项的 SSOT。每项注册后:
  1. 周检自动从注册表读取、计算样本量/状态/离毕业还差多少
  2. 新增影子必须在此注册 (否则无人跟踪 = 等于没做)
  3. 状态变迁必须在此更新 (用户裁决结论作为审计留痕)

生命周期: OBSERVING → READY_TO_EVALUATE → GRADUATED/KILLED/SUPERSEDED
  OBSERVING:           n < min_n, 继续攒样
  READY_TO_EVALUATE:   n >= min_n, 等待评估/用户裁决
  GRADUATED:           评估通过, 已转生产 (记录何时/何值)
  KILLED:              评估否决, 门/特性已关闭 (记录否决理由)
  SUPERSEDED:          被新版取代 (指向替代项)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from enum import Enum

# 挂载核对哨兵的 cron 解析面 (2026-09-01 复查收口): /etc/cron.d 内含点号文件名同样被
# Alinux8 cronie 执行 (401bedcb 僵尸 cron 38 天教训), 核对面必须覆盖目录内全部文件,
# 不能只看 crontab -l。测试可 monkeypatch 这两个常量注入临时目录。
_ETC_CRONTAB = Path("/etc/crontab")
_ETC_CRON_D = Path("/etc/cron.d")

_log = logging.getLogger(__name__)


class ShadowStatus(str, Enum):
    OBSERVING = "OBSERVING"
    READY_TO_EVALUATE = "READY_TO_EVALUATE"
    GRADUATED = "GRADUATED"
    KILLED = "KILLED"
    SUPERSEDED = "SUPERSEDED"


@dataclass
class ShadowItem:
    id: str
    name: str
    status: ShadowStatus
    description: str
    data_path: str
    # 🔴 config_key **可能是生产键, 不是影子开关** (2026-09-26 RFC-109⑤-C, 用户审计陷阱实证):
    # 照 config_key「停采」前必须验**该键是否真门控 data_path 的影子写方** (grep 写方源码),
    # 两种翻车形态都有实锤:
    #   ① volume_gate_vwap_relative (p1_3_vol_p50) = **生产量能门** (现 true) ⇒ 照它停采 = 关生产闸;
    #   ② mfe_stall_exit_enabled (mfe_stall_exit) = **生产退出腿** (现 false) 且**不门控**影子写方
    #      (写方 exit_engine:860 在 ATR≥4%∧peak<0.3%∧profit<0 分支内无条件写) ⇒ 它 false ≠ 影子已停。
    # 当日实测教训: 盘点脚本按「config_key 现值 false ⇒ 真停」判, 把 ② 误报成已停 —
    # notes 里的自由文本警告没拦住 ⇒ 规则必须放字段级。
    # 真影子开关的正例 (可安全用): falsification_gate_capital_flow_shadow (pipeline:6274 真门控写方,
    # false 后产物冻结 1594 行) / limit_entry_shadow_observe (pipeline:6310) / llm_features.*_shadow.enabled /
    # reentry_quick_tp_shadow_enabled (reentry_tp_shadow.py:42)。
    config_key: Optional[str] = None
    min_n: int = 30
    current_n: int = 0
    graduation_criteria: str = ""
    eval_script: Optional[str] = None
    created: str = ""
    last_evaluated: Optional[str] = None
    verdict_history: list = field(default_factory=list)
    # 采集退役标记 (2026-09-21): True = 该通道**不再采集**。
    # 刻意与判据状态分开: 判据退役 (KILLED/FAIL) **不等于**没有消费者 —— 实锤 capital_flow
    # 的原料在喂生产门 (intraday_reentry 盘中资金背离门), 若按判据状态自动停采会断料。
    # 停采必须**显式**声明 (本字段或该通道独立开关), 并在 notes 写理由; 数据一律保留不删。
    #
    # 🔴 **适用面警告 (2026-09-26 RFC-109⑤-B 实测更正 — 原注释承诺过宽)**:
    # 本字段的**执行者全仓只有 1 个**: scripts/futu_channel_shadow_collect.py:70 `_collector_gate()`
    # (它同时服务 futu_channel_* 四条通道)。实测 `grep -rl collection_retired app/ scripts/ ops/`
    # ⇒ 只有该采集器 + shadow_board.py (后者只**读来显示**「·已停采」并抑制停摆告警, 不停任何东西)。
    # ⇒ 对其余 13 条终态影子 (写方在 pipeline.py / exit_engine.py / 独立 scripts) 设 True 是
    #   **假停采**: 产物照样每天写, 而看板会显示「已停采」且**抑制停摆告警** ⇒ 账面说停了、
    #   实际没停、还把证据藏起来 (活谎言结构, 比不设更危险)。
    # ⇒ 非 futu_channel 影子的真停采路径只有两条 (实测已生效的 2 条先例):
    #   ① 该影子自己的 config 键置 false (如 p1_1_capital_flow 靠
    #      falsification_gate_capital_flow_shadow=false, 产物冻结在 1594 行);
    #   ② 摘 cron 行 (如 overnight_earnings_carry 靠删 cron, 产物冻结在 9/23)。
    #   两条都必须在 notes 写明**实际生效的机制**, 本字段只作账面标记 (并让看板不再误报停摆)。
    # ⇒ 设本字段前必须验: 该项写方是否读它 (grep 写方源码), 或另有真机制。
    collection_retired: bool = False
    notes: str = ""
    # 计数口径: "lines" (默认, jsonl 行数) | "distinct:<field>" (按字段去重计数,
    # 用于首日回补大量历史数据的事项 — 回补行只算 1 个样本, 防立即超 min_n
    # 触发永久 READY 通知风暴; 2026-08-29 futu_channel_iv_hv 落地时引入)
    count_mode: str = "lines"
    # eval 执行方式: False=应有 cron/调度挂载 (挂载核对话语权); True=按需手工/
    # 裁决日触发 (哨兵跳过挂载核对, 2026-09-01 挂载核对哨兵引入)
    eval_on_demand: bool = False
    # 产出超龄属预期 (已停采/read-only 按需重算): True ⇒ check_mounting 跳过
    # 「产出超龄」告警; 文件缺失仍照常告警. 2026-10-18 减法批引入:
    # futu_channel_iv_hv (9/21 已停采, 10/21 重看) / ticket_size_breakeven
    # (read-only 按需重算) 的 27d/15d 超龄是预期状态, 不是断流.
    stale_expected: bool = False
    # 判据不可裁决时的显式挂账原因 (2026-09-21 清账批 RFC-092 引入): 非空 ⇒
    # refresh_counts 不迁移 READY_TO_EVALUATE (防周检把"样本量达标但无判据可跑"的项
    # 当成"待用户裁决")。取值形如 "PERMANENT_MEASURE: ..." (无毕业门, 永久测量项) /
    # "NO_EVAL_SCRIPT: ..." (判据已预注册但无执行体, 待建脚本或退役)。
    no_adjudication_reason: str = ""
    # 意图归组 + 互引 (2026-09-11, DEB-20260911 共识 C9/N3, 用户裁「按建议执行」):
    #   立起理由 — A_short_chase (盘前快照口径) 与 short_chase_ban_shadow (触发时刻口径)
    #   同意图「逆势追空禁入」同阈值 +3%, 双影子独立攒样独立裁决、台账零互引, 且已在真实
    #   数据给出矛盾结论 (BIDU 9/8 同指令 7 秒内 -7.65% vs +4.07) ⇒ 存在"一个 FAIL 一个
    #   PASS → 双转正打架"风险。现有守卫只强制**存在性登记**, 无机制强制**意图唯一性**。
    #   intent: 交易意图标签 (同标签的现役事项必须互指, 见 check_intent_uniqueness);
    #   cross_ref: 与本源同意图/同假设的其它注册表 id (含已退役/被取代方, 供裁决日对照)。
    intent: str = ""
    cross_ref: tuple = ()


# ── 已读判据登记 (2026-09-21 RFC-092 清账批 + §10.3 字段级修法) ──────────────────
# 为什么需要它: `refresh_counts()` 只按 `current_n >= min_n` 机械迁移 READY_TO_EVALUATE,
# 而该状态的真实语义是"**样本量**达标" —— 判据可能早被测过且**未达标**(自动续观即可),
# 于是"READY"在通知面/呈现面上被读成"待人裁决的欠账"。
# 本登记把"已经读过判据"这件事写进 SSOT: 有登记且未到 `recheck_after` ⇒ 该项是"已读数待攒样",
# 不构成待裁; `needs_user=True` ⇒ 判据已有读数但**处置需要用户裁决**(如通道用途未定)。
# 纪律: 改判据 = 重新裁决, 不许靠改本登记的日期把"待裁"改成"已裁"; 日期只是重看闹钟。
# 取值口径: read_at = 判据被实测读出结论的日期 (须能追到 RFC/DEB 或实跑记录);
#           recheck_after = 下次应重看的日期 (默认 +30 天; 观察窗类填窗口结束日);
#           needs_user = 判据读数已出但处置待用户裁 (与"自动续观"区分)。
ADJUDICATED: dict[str, dict] = {
    "futu_channel_top_movers": {
        # 2026-10-03 闹钟到期, 用户确认执行 9/21b 建议③ (并入 kr3 轴, 不新增门/影子)。
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "榜内横截面 IC=-0.44/t=-13 (榜首续跌) 与笔级 top10 +$15.20/笔 符号相反 ⇒ 用途待裁 "
                "(RFC-092 §8.2/§9.1)。用途三条路 (排除/反向池/并入 kr3) 均需另立预注册。"
                "2026-09-21 用户裁: 9/30 窗满后一次裁, 在此之前维持现状 (榜单继续做参考层输入)。"
                "│9/26 重跑读数追加 (RFC-109④, 裁决与闹钟不变): 20 会话/4609 对/逐日IC有效19天, "
                "IC 均值 **-0.4474 t=-15.877** MDE 0.0789 ⇒ 可分辨; 十分位前向 top10 -0.2004 / "
                "mid +0.6875 / bottom10 +0.7093 (榜首组最弱=反向); 30min 窗均值 +0.4286%。"
                "⚠️ 尺子缺陷已修: 该脚本 verdict 判据是 abs(t)>=2 ⇒ 负 IC 也印 "
                "IC_SIGNIFICANT_GRADUATE_CANDIDATE, 照抄会把「该反向用」读成「可接候选池(同向)」; "
                "已在 stdout 加 [只印不判] IC 方向行并列十分位 (判决字段一字未动, 改判据=重新裁决)。"
                "│2026-10-03 终裁: ③并入 kr3_direction_runup_gate 轴, 本影子不转正/不加门; "
                "榜内横截面 IC 仅作 kr3 解读佐证。采集维持 (9/21 用户已裁继续采, "
                "覆盖是③轴瓶颈)。",
    },
    "reentry_quick_tp_gate": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "窗满机械结算 (判据字典 B13; verdict_history 同日): 13 行 reentry 实弹, "
                "tp_touched=0/13 ⇒ 判据轴「TP 触达 n≥10」实际 n=0, 落「否则」分支 — "
                "门禁维持, quick_tp 参数在现行门禁下零触发。quick_tp 参数的删除属 L3 "
                "待用户裁 (docs/decisions/REVIEW-20261003-shadow-cleanup.md), 影子本体已 "
                "KILLED 停采。〔前读 2026-09-21: n=13/10 达标, 窗内禁改, 到期一次性结算。〕",
    },
    "premarket_reprice_shadow": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "窗 8/18-9/30, settled=58 (hold 26 / tighten 15 / be_raise 17)。"
                "「按盘前价离场 vs 实际」差: hold -$756.83 (-$29.11/笔), "
                "tighten -$397.72 (-$26.51/笔), be_raise +$31.04 (+$1.83/笔); "
                "be_raise 组 15/17 触达 BE 线但总改善仅 $31 ⇒ 无可复现净改善, "
                "KILLED 停采 (DEB-20260818-overnight-reprice-shadow)。",
    },
    "regime_divergence_shadow": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "54 行/51 交易日全部 all_agree=False, 但采集尺本身失真: 只有两个模块"
                "产出标签, MarketRegimeDetector 无参 detect 用 vix=20/spy_change=0 "
                "占位 (DEB-20260819-symbol-pangan); 不能按分歧率裁「语义正交/合一」。"
                "无活消费者 ⇒ KILLED 停采, 数据留档。",
    },
    "reentry_gate_counterfactual": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "输入 data/intraday_reentry_shadow.jsonl 已 2026-09-17 停供, 最新行"
                "9/16; 本尺 10/3 仍在重算同一份 3756 拒/1770 可评。与 "
                "crontab-reentry-gate-effect 同型空转, KILLED 停采 "
                "(docs/CONTROL-LOOP-LEDGER.md)。",
    },
    "direction_counterfactual_ruler": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "原始问题「方向是否稳定偏向/有 edge」已有终读: BUY intraday/T+1/T+3/"
                "T+5 均落 45-55 掷币带; SELL_SHORT 4 档 3 档掷币带, 仅 intraday "
                "55.7% 正指但均值 -0.016% (DEB-20260816 §19.2)。产物无活读者 ⇒ "
                "KILLED 停采, 历史可手动复跑。",
    },
    "t5_hold_counterfactual": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "507 笔可评: T+5 反事实 -$13,436 vs 实际 -$2,038, Δ-$22.48/笔, "
                "中位 Δ-$13.80; 描述带 ≤-$10 判「不支持摆动」。产物无活读者 ⇒ "
                "KILLED 停采 (DEB-20260822-health-check-t5-counterfactual)。",
    },
    "ue_confirm_gate": {
        "read_at": "2026-10-22", "recheck_after": "2026-10-31", "needs_user": False,
        "note": "2026-10-22 最新报告: 39 交易日/171 裁决/167 join; PRICE_IN n=73 "
                "mean +5.52% (median +0.22%), CONFIRM n=85 mean -0.09% (median -0.06%) "
                "⇒ 判据两项方向均不过, verdict=KEEP_SAMPLING; 10/31 重看 "
                "(scripts/settle_ue_confirm_gate.py 产物)。"
                " 2026-10-22 用户确认 B: 保留 21:10 UE scan + score; confirm 按预注册"
                " 10/31 重看; 不改 cron。",
    },
    "eth_ext_entry_prefilter": {
        "read_at": "2026-10-03", "recheck_after": "", "needs_user": False,
        "note": "n=5/30, 末行 2026-09-18; EXT_PREFILTER_SHADOW 默认 false 且现行"
                "crontab 未传 ⇒ 已停采。同人群价格轴由 ext_entry_exec_price_shadow "
                "接管 (546 轮, $5→$50 二分反事实), 判 SUPERSEDED "
                "(docs/debates/DEB-20260928-ext-exec-price50-shadow.md)。",
    },
    "ue_short_boost_shadow": {
        "read_at": "2026-10-15", "recheck_after": "", "needs_user": False,
        "note": "2026-10-15 结案: 用户裁「冻结样本结算后归档」。一次性结算 "
                "scripts/ue_short_boost_final_settlement.py 52/54 可结算: "
                "T+1 方向调整 mean +0.0523% / t=0.667 / MDE80=2.94pp (无可测 edge), "
                "尾部 15 笔 −0.4618%; 主门 n=60 永不可达 (写方链 10-06 退役) ⇒ KILLED。 "
                "判据出处 DEB-20260805-supply §四.3; 产物 data/ue_short_boost_final_settlement.json。",
    },
    "gap_chase_gate": {
        # 〔2026-10-22 重看: live n=34/30; 09-21 读数 (n✓/笔均✓/符号检验 p=0.93✗, 3 项过 2)
        #   结论不变, 维持攒样。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "n=31 ✓ / 笔均 -16.6 ✓ / 符号检验 p=0.93 ✗ ⇒ 3 项过 2, 继续攒样 (判据字典 B1)。",
    },
    "vwap_gate_ab": {
        # 〔2026-10-22 重看: live n=239/30; 09-21 verdict「不可分辨, 不改门」维持。
        #   10-22 起 FADE_SHORT 轴由 RFC-158 预注册接管 (每 archetype 每臂 n≥30; 当前
        #   FADE_SHORT 处理 5/对照 10, 数月量级); 5 日扩样读数: 处理组 −0.346% vs 对照
        #   +0.164% ⇒ 门总体倾向拦对, 10-21「门反向」小样本假设被推翻。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "补齐按日聚类后被拦组 +0.176% 但 MDE±0.39 ⇒ 不可分辨, 不改门 (RFC-092 §8.4)。",
    },
    "entry_delay_confirm": {
        # 〔2026-10-22 重看: live n=1258/20; 09-21 读数=前瞻子样本全不过 + 日聚类
        #   MDE±7.95 不可分辨 ⇒ 不呈裁, 结论不变。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "全样本过线但前瞻子样本全不过 + 日聚类 MDE±7.95 ⇒ 不可分辨, 不呈裁。 (读数出处: RFC-092 §5)",
    },
    "exit_disable_cf": {
        # 〔2026-10-22 重看: live n=75/50; 09-21 读数=每规则 n 门未达 (MFE 31<50 /
        #   DEAD 33<50), 判据轴=每规则分桶 ⇒ 结论不变, 攒样。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "每规则 MFE n=31<50 / DEAD n=33<50 ⇒ 每规则 n 门未达。 (读数出处: RFC-092 §5)",
    },
    "extreme_gap_ban_shadow": {
        # 〔2026-10-22 重看: live n=642/30; 09-21 读数=被拦净 −1826 ✓ 但误杀率 53%>40% ✗
        #   ⇒ 判据未过维持纯观测, 结论不变。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "被拦 n=58 净 -1826 ✓ 但误杀率 53%>40% ✗ ⇒ 判据未过, 维持纯观测。 (读数出处: RFC-092 §5)",
    },
    "gap_alignment": {
        # 〔2026-10-22 重看: live n=642/30; 09-21 读数=格内 n 门未达 (5/10/22/9… 多 <30),
        #   判据轴是分格样本不是总行数 ⇒ 结论不变, 攒样。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "各格 n=5/10/22/9… 多 <30 ⇒ 格内 n 门未达。 (读数出处: RFC-092 §5)",
    },
    "intraday_loss_breaker_a_prime": {
        # 〔2026-10-22 重看: 处置已于 10-16 固化 (verdict_history: DO_NOT_ENABLE,
        #   DEB-20261016) ⇒ 不再周期重看; 重启该假设须新预注册。〕
        "read_at": "2026-10-22", "recheck_after": "", "needs_user": False,
        "note": "被挡笔期望 +45.0 (门 ≤-10, 方向相反) 但 forward 仅 14<30 ⇒ 按已记录规则不下结论。 (读数出处: RFC-092 §3 #5)",
    },
    "kr3_direction_runup_gate": {
        # 2026-10-04 更新 (10 月开跑会 B1, 用户裁「按建议执行」): LONG 腿期末判分已出
        # (FALSIFIED 干净证伪), SHORT 腿 §三.5 样本门未达 (n=23<30) → 延长观察, 闹钟提前至
        # 10/17 (实测速率 9/4 以来 0.57 笔/日, 已结算 24, 还需 6 笔 ≈ 11 天 → ~10/15 达门,
        # 闹钟留 2 天余量; 到点重跑 scripts/kr3_final_adjudication.py)。
        # needs_user=False: SHORT 腿 verdict 出来后按预注册自动落档, 处置 (停采/立案) 才需用户。
        # 2026-10-15 结案: n=30 恰好于预测日达门, 当日重跑裁决批 → SHORT FALSIFIED (三要素 1/3),
        # 用户裁「FALSIFIED 落档+停采」 ⇒ 本闹钟结案 (recheck_after 清空)。
        "read_at": "2026-10-15", "recheck_after": "", "needs_user": False,
        "note": "2026-10-15 终裁 (闹钟 10/17, 样本门 10/15 恰好达成当日结算): SHORT|blocked n=30 "
                "三要素 1/3 (日聚类t=-0.2994 / 声称-21.83<MDE80=56.45 / OOS同向✓) ⇒ FALSIFIED; "
                "LONG 腿 10/4 已 FALSIFIED ⇒ KR3 两腿全证伪, 按 OKR-2026-10「确认或干净证伪都算达成」"
                "KR3 达成; 用户裁「FALSIFIED 落档+停采」 ⇒ 采集 cron 注释退役 + 注册表 KILLED + "
                "OKR 变更记录回写 (证据: registry verdict_history 10/15 条目 + "
                "data/shadow/kr3_final_adjudication.json + docs/OKR-2026-10.md 变更记录 10/15 行)。"
                "│期末裁决批 2026-10-04 (scripts/kr3_final_adjudication.py, 现写现验): "
                "LONG|blocked n=43 三要素 0/3 (日聚类t=-0.02/声称+2.10<MDE32.88/OOS 翻转-4.00→+10.36) "
                "= **FALSIFIED 干净证伪**; SHORT|blocked n=23<30 = §三.5 不裁决延长观察 "
                "(观测: OOS 同向-45→-43, 尾部占比 86.4%, 只记录不结论)。证伪不立即换假设 (KR3 明令); "
                "SHORT 腿攒至 n≥30 重跑同脚本, 闹钟 10/17。 (读数出处: registry verdict_history 10/4 条目 "
                "+ data/shadow/kr3_final_adjudication.json + docs/OKR-2026-09.md 变更记录 10/4 行)",
    },
    # ── 2026-10-15 全链路审计 P1-3 对账批新增 ──
    "eth_held_exit_shadow": {
        "read_at": "2026-10-15", "recheck_after": "2026-10-30", "needs_user": False,
        "note": "在采未注册(禁止态)补登 + 同日裁决闭环: 用户裁「追认」(RFC-127 §7 知情破例"
                "留痕) — 10-09 由 80d8817ba 携带上线的阶段1 变体语义 (release 移交盘中) "
                "追认为现役; 影子重定位=移交失败监测面, 10-30 重看失败子集读数。",
    },
    "post_ca4487": {
        "read_at": "2026-10-15", "recheck_after": "", "needs_user": False,
        "note": "在采未注册(禁止态)补登。观测窗在跑: 10-14 收盘 n_closed=28/200, single_version, "
                "hash 缺失 0。无可裁决事项 (判决面=oct_cohort BROKEN 终局已定); PERMANENT_MEASURE "
                "挂账防机械迁 READY。窗口进度重看=OKR 结算日 (10-31)。"
                "出处: DEB-20261009-event-to-trade-pipeline §28.11 + docs/OKR-2026-10.md KR1 回写行。",
    },
    "ext_entry_earnings_criterion_shadow": {
        "read_at": "2026-10-15", "recheck_after": "2026-10-31", "needs_user": False,
        "note": "在采未注册(禁止态)补登。在采健康 (10-15 07:55 仍写, 5765 行/16 ET日, "
                "legacy_blocked_new_pass=493)。判据②读数面=10 月财报季裁决「修口径放回多少票」, "
                "故重看窗=10-31 (财报季末/OKR 结算窗)。出处: RFC-097 §预注册判据 "
                "(docs/decisions/RFC-097-earnings-criterion-anchor-and-entry-band.md)。",
    },
    "entry_anchor_dual": {
        # 〔2026-10-22 重看: live n=2178/300; 预注册第一腿「影子期≥10 交易日」已自满 (~10-20)。
        #   ⚠️缺口: 结算器仍缺 (eval_script=None, 10-15 注记「阶段1 裁决前必补」) ⇒
        #   列 P2 建结算器, 建成前不判。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-05", "needs_user": False,
        "note": "n 轴达标 (anchor_recorded=1120≥300) 但预注册第一腿「影子期≥10 个交易日」未达 "
                "(现 6 日, 10-07 起, ~10-20 满) ⇒ 不构成待裁; 结算器仍缺 (eval_script=None, "
                "阶段1 裁决前必补, RFC-123 阶段1 未授权不动)。",
    },
    "penetration_shadow": {
        # 〔2026-10-22 重看: live n=203/20; 09-21 读数=假设集 None (未调 LLM) 不判 ⇒
        #   维持; 若要用穿透率尺子须先补假设集。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "基线穿透率 97.4%; 假设集 None (未调 LLM) ⇒ 不判 (判据字典 B17)。",
    },
    "premarket_invalidation": {
        # 2026-09-21 用户裁「口径随实现」(批准记录见 handoff 当日下午增量):
        # 判据文字"盘前口径增量"更正为"开盘后早段盘中 (09:35–09:59 ET) 口径"——
        # B 规则判"开盘后跌>5%"事件, 盘前相位不存在该事件, 实现相位本就正确, 系登记文字过窄。
        # 改判据=重新裁决留痕: graduation_criteria 同步更正 (下方条目内【口径更正】标记);
        # B 已攒 n=4 均为盘中相位采集, 更正后口径下有效, 不清零继续攒样至 n>=20。
        # "盘前数据独立增量"成为新问题, 若要问须另立预注册 (本次未开)。
        # 〔2026-10-22 重看: 注册表行数 53 是打标行; 判据轴=B 规则 executed n (9-21 读数 n=4<20)
        #   ⇒ 攒样中, 结论不变。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "A/C 已按 DEB-20260911 处置 RETIRED_NO_PROMOTION; B 执行 n=4<20 ⇒ 攒样中。"
                "相位口径已定谳 (2026-09-21 用户裁): 开盘后早段盘中 09:35–09:59 ET。"
                "实测证据: 产物 396 条全部落在 09:35–09:47 ET (RFC-092f,"
                " docs/analysis/2026-09-21-realtime-price-authority-audit.md §3)。",
    },
    "da_blocked_cf": {
        # 〔2026-10-22 重看: live n=253/50; 09-21 读数 T+1 −0.051% 未越 ±0.15% ⇒
        #   KEEP_SAMPLING 结论不变。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "T+1 mean -0.051% 未越 ±0.15% ⇒ KEEP_SAMPLING (判据字典 B14)。",
    },
    "futu_channel_iv_hv": {
        # 〔2026-10-22 重看: live n=317/30; 09-21 verdict=NO_RESIDUAL (MDE±52 不可分辨) 结论不变 —
        #   MDE 数学不随增量翻案; 维持观测。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "三桶无残存效应 (medium +35.74 但 MDE±52 不可分辨; high 无效应) ⇒ NO_RESIDUAL (RFC-092 §8.1)。",
    },
    "futu_channel_ofi_baseline": {
        # 〔2026-10-22 重看 (过期闹钟清理批): live n=3246/600 已 5× 越门; 09-21 挂账=OFI 自身 IC
        #   从未单独测 (只测过 capital_flow 相对增量 -0.1192) ⇒ 该测量仍欠, 列 11 月测量任务,
        #   测前不构成待裁。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "只测到 capital_flow 相对它无增量 (-0.1192); OFI **自身** IC 未单独测 ⇒ 无 KILL 依据。 (读数出处: RFC-092 §5)",
    },

    # ── 2026-09-26 RFC-109④ 清账批: 9 条待裁面逐项实跑 eval 后的读数登记 ──────────
    # 纪律: 每项都是**本批实跑**得出的读数 (非引注册表状态/🔔 横幅); read_at=实跑日。
    # 不改任何 graduation_criteria/min_n (改判据=重新裁决)。needs_user=True 仅用于
    # 判据达成但处置需用户裁的项。
    "futu_channel_capital_flow": {
        # 补登欠账: 9/21 用户裁「不停采」已写在本条 notes (🔒 段), 但从未入 ADJUDICATED
        # ⇒ 周检每周把这条已裁项重复通知为「待裁」(skill 明载病灶: 已出读数不改状态
        # ⇒ 通知面 ≠ 真实待裁面)。本行只把既有裁决登记进 SSOT, 判据与采集均零改动。
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "9/26 重跑 futu_channel_shadow_eval: rank_ic_marginal=-0.1447 (t=-1.52, "
                "n_pairs=110) 不显著; 增量 vs OFI=-0.1326; OOS 双窗同号 (-0.1953/-0.0971) "
                "但均为负; verdict_candidate=WEAK_EVIDENCE ⇒ 毕业判据 (双窗 OOS ≥40% 或 "
                "ρ≥0.15) **不过**。可复跑: scripts/futu_channel_shadow_eval.py。采集维持不停 (9/21 用户裁 + 原料喂生产盘中背离门 "
                "intraday_reentry, 见本条 notes 🔒 段)。要动的是判据/门方向, 不是采集。",
    },
    "exit_param_shadow": {
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "9/26 实跑 exit_shadow_graduation (N=495, 三门: N≥30 / 均值改善 / 一致性≥55%): "
                "BE/DD 族 5 变体全 FAIL (最好 be0.3_dd0.3 Δ+0.191 但一致性 38.2%<55%); "
                "30min-abort FAIL (mean_saved -0.0299%); early_tight 族 5 变体全 FAIL "
                "(一致性 45.9-47.5%, 均值虽正 +0.20~0.26% 但不过一致性门) ⇒ **无新变体可毕业**。"
                "唯二 PASS = dead_signal_60m_mfe0.2 (N=465, triggered -0.915% vs not +0.296%, "
                "分离 +1.212pp) 与 mfe0.3 (+1.157pp) — 对应**已毕业的生产退出腿** "
                "(dead_signal_exit_enabled=true/60min, 8/10-9/21 实跑 36 笔 -$309; 可复跑: scripts/exit_shadow_graduation.py) ⇒ 属既成"
                "事实确认, 非新提案。判据不过的变体维持纯观测, 不改任何退出参数。",
    },
    # ── RFC-139 复查修复批 P4 (2026-10-08): entry_band_widen 假待裁修复 ──
    # 病灶: 10/04 结算器首跑已出读数 NOT_ESTABLISHED_KEEP_SAMPLING (RFC-065 判据:
    # 安全门过但正向证据不足 ⇒ 顺延攒样不翻开关), 10/07 DEB-20261007 方案B 又把判据串台
    # 修回 RFC-065 原文 — 但始终未登记 ADJUDICATED ⇒ needs_adjudication 恒 True,
    # 每周被抬进「① 需要你决定」段, 而 lexicon cls=AUTO_WATCH 的背景块同时印
    # 「你要决定: 暂无」⇒ ①段标题与内容自相矛盾 (正是它想消除的「混淆决策源」)。
    # 本登记只把既有裁决收进 SSOT (同 9/26 清账批先例), 判据/采集/结算器零改动。
    "entry_band_widen": {
        "read_at": "2026-10-04", "recheck_after": "2026-11-04", "needs_user": False,
        "note": "10/04 首跑结算器 (scripts/settle_entry_band_widen.py, RFC-065 §13/§29.3 预注册): "
                "n_settled=36/30 达门 ✓, 单侧安全门过 (perm p_worse=0.0586>0.025, 不显著劣于 flat) ✓, "
                "但正向证据不足 (mean δ=-$17.73 / bootstrap CI95=[-40.0,1.68] 含 0 / 符号 p=0.309) ✗ "
                "⇒ verdict=NOT_ESTABLISHED_KEEP_SAMPLING — 按预注册顺延攒样, 不翻开关。"
                "10/07 DEB-20261007 方案B: graduation_criteria 串台修复回 RFC-065 逐字原文, "
                "min_n 60→30, 判决不变 (判据原文未动)。可复跑: scripts/settle_entry_band_widen.py "
                "(产物 data/reports/entry_band_widen_verdict_latest.json)。",
    },
    "universe_expansion_shadow": {
        # 2026-09-26 用户裁「退役」(判据 FAIL_CRITERIA_MET) ⇒ status KILLED, needs_user=False
        # 退出待裁面; recheck_after=None = **已终裁不周期性推回** (同 eth_exit_forgone_upside 先例:
        # 未来若重启须新预注册判据, 复用本判据 = 阈值购物)。
        # ⚠️ 不设 no_adjudication_reason (会永久屏蔽重看窗, 且本项已终裁无需该字段)。
        # 🔴 capture cron 未摘 (UE 生产 prompt 原料) ⇒ 本登记**不代表采集已停**, 详见注册表 notes。
        "read_at": "2026-10-22", "recheck_after": None, "needs_user": False,
        "note": "判据 (逐字借用 ue_midday_shadow_eval §13.3, 2026-08-14 用户裁) 裁决桶 h=1: "
                "ALL n=98/19日 命中率 38.8% (门55%) ✗ 均值 −0.481% (门+0.5%) ✗ p=0.9902; "
                "LONG n=33 命中率 12.1% 均值 −2.043% p=1.0000 ⇒ 样本轴双过但效果不达标 "
                "= FAIL_CRITERIA_MET ⇒ 用户裁退役。0.5%/1.0% 双阈值同判 (非阈值挑选)。"
                " 执行: score+eval cron 已摘, **capture 保留** (llm_universe_scan "
                "_gather_market_corroboration fallback 只读 type=capture, DEB-20260914 实测 "
                "da_feed 窗结构上永远早于 DA ⇒ 次次走回退, 且 inject+entry_enabled=true ⇒ "
                "摘它=改 UE 交易行为超授权)。写者无 config 开关 ⇒ 摘 cron 是唯一真停采机制; "
                "不设 collection_retired (语义矛盾 ⇒ 会变成假停采+抑制告警)。"
                " **未处置**: UE 通道方向不对称 (LONG −$841/SHORT +$276) 不在本裁决范围 "
                "(人群重叠仅 9 标的/23.3% 笔数), 归 ue_tier_gate 备证线。"
                " 2026-10-22 用户复核 (选项 A): **保留 capture cron**; 复核确认 "
                "_gather_market_corroboration fallback 消费 capture (今日 08:00 UE 日志实走回退), "
                "KILLED 判据只针对影子预测值, 不覆盖 capture 作为 UE prompt 市场佐证原料; "
                "未改 crontab (第 91 行仍在), 无行为变更。",
    },
    "early_dead_signal_shadow": {
        # 2026-09-26 用户裁 **A 案: 维持纯观测, 不接线, 不改判据词表** ⇒ needs_user 落 False
        # (已裁完, 退出待裁面); recheck_after 保留 10/26 = 届时重看现役词表基率与反事实读数
        # 是否漂移 (overdue 会把它重新推回待裁面), 不是重开裁决。
        # ⚠️ 不设 no_adjudication_reason: 该字段在本注册表的既有语义是**结构性挂账**
        #   (NO_CRITERIA / PERMANENT_MEASURE / NO_EVAL_SCRIPT), 且 refresh_counts 的提升条件含
        #   `not s.no_adjudication_reason` ⇒ 一旦设上就**永久屏蔽重看窗**, 与上面保留的 10/26 冲突。
        #   「已裁完待重看」用 needs_user=False + recheck_after 表达即可 (capital_flow / exit_param /
        #   eth_premarket 等已裁项同型)。
        # ⚠️ status 保持磁盘上的 OBSERVING, **不要**试图改成 READY: refresh_counts() 对
        #   「OBSERVING ∧ current_n≥min_n ∧ 无挂账原因」会运行时自动提升为 READY_TO_EVALUATE
        #   (代码注释明写「READY 仅表示样本量达标, 不代表判据通过」) ⇒ 本项运行时读数就是 READY,
        #   与其余 needs_user=False 的已裁项完全一致, 属既有设计不是不一致。
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "9/26 实跑 early_dead_signal_report (已结算 389): 判据①双窗死单率均≥55% ⇒ "
                "**FAIL** (预注册词表 15min 1/111=1%, 30min 0/86=0%; 词表现役基率仅 1.3% "
                "vs 预注册当时 30% = 词表漂移, engineering-change-protocol 2026-09-23 实锤). "
                "判据②命中组净 PnL<0 ⇒ **PASS** (15min -$3245 / 30min -$2763). "
                "判据③差集结局为负 ⇒ **PASS** (73 笔 -$2517 / 43 笔 -$1986). "
                "现役词表 lift 只印不判: 30min 命中组 40.7% vs 基率 11.0% = **3.71×**, "
                "15min 2.55×。│9/26 二批 (RFC-109④-B, 提案 PROPOSAL-20260926) 补两面读数: "
                "①按判据的**预注册相对意图** (55%线÷当时基率30%=1.83×, 报告 9/23 注释载明) "
                "重算 + Fisher exact ⇒ 三档现役词表**双窗全部过相对线且全部显著** (现役死信号 "
                "2.55×/3.71× p=5.3e-05/2.2e-09; +耐心出场 2.54×/3.71× p=4.5e-06/7.2e-12; "
                "+收口跳空 2.24×/3.08× p=3.0e-06/1.2e-11) ⇒ 信号确有信息量; "
                "②🔴**反事实回测证伪接线价值** (判据①②③ 答不了「早退该不该接线」, 因早退本身"
                "要花钱; 影子行自带 entry_price+cur_pct ⇒ 按用户 8/14 口径直接回测不攒样): "
                "net_cf=qty×entry×cur_pct%−fees_est, Δ=net_cf−final_pnl_net ⇒ 30min 命中组 "
                "(去重86笔) 亏→盈(早退砍反弹) 47笔/55% Δ**-2927** vs 亏→亏(早退少亏) 33笔/38% "
                "Δ+2365 ⇒ **净额 Δ=-595$**; 15min 同构 (61笔/56% -3223 vs 40笔/37% +3194 ⇒ "
                "**-415$**)。决定性: 两类在检查点时点**不可区分** (cur_pct 均小负) ⇒ 不能只挑 "
                "38% 该救的接线, 一接线必连 55% 反弹单一起砍。差集 (60min 死信号腿未抓到=唯一"
                "可能新增量) Δ 15min -141(t=-0.14)/30min +178(t=+0.21) ⇒ **差集也无增量**; "
                "已被该腿抓到的组 Δ-274/-773(t=-2.75) ⇒ 早退更差, 反证既有生产腿时点合理 "
                "(无同轴重复空间)。⇒ 判据①按预注册词表不可能达成 (漂移非影子失效), 而按现役词表"
                "lift 3.7× 强过线 ⇒ **处置需用户裁**。│二批建议 **A 案: 维持纯观测, 不接线, "
                "不改判据词表** — 信号有信息量但接线净期望为负 (按用户 8/22 口径: 机制须救得回"
                "费用线, 本门不满足); B 案改词表须另立 DEB (改判据=重新裁决) 且**改完仍不能接线**"
                "(反事实与词表选择无关: 无论用哪个词表定义「死」, 早退净期望都是负的) ⇒ 属为过线"
                "而过线, 不建议; C 案缩窄门需新预注册+新攒样, 且「亏→盈」与「亏→亏」在检查点"
                "不可区分 ⇒ 现有数据无事前分层变量, 要做须先证明存在能区分两类的**事前**特征 "
                "(入场时 ATR/regime 等), 那是新研究不是裁决。本批不动判据文字。│"
                "**2026-09-26 用户裁 A 案 (维持纯观测, 不接线, 不改判据词表)** ⇒ 本项已裁完, "
                "不再入待裁面。留痕: 采集与 settler cron (2 6 * * 2-6) 均**不动** (纯观测就是"
                "继续采), 生产退出腿零改动; 10/26 重看窗只核「现役词表基率 / 反事实净额」是否漂移, "
                "漂移才重开。若日后要接线, 前置是先证明存在能区分「亏→盈」与「亏→亏」的**事前**特征 "
                "(B/C 案的共同前提), 那是新研究须另立 DEB。",
    },
    "eth_premarket_exit_shadow": {
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "9/26 实跑真结算器 settle_extended_hours (新结算 270 行, 累计 5479): "
                "判据「水下保本族/时间退出族净额>0 且样本≥30」⇒ 盘前 tighten n=125 "
                "avg_t0_close_ret **+0.369%** (>0 ✓) / hold n=139 **-0.302%** (✗) / "
                "hold_no_stop n=2312 -0.301% (✗); lock_in n=2 与 breakeven_lock n=2 "
                "样本不足。⇒ tighten 族过线但其余族净额为负 ⇒ 不构成「提案」条件 "
                "(判据要求族净额>0)。⚠️ 尺子挂错轴已发现: 注册表 eval_script 填的是"
                "**每分钟采集器** extended_hours_exit_cron.py (实跑返回 "
                "skip=outside_extended_hours, 非评估器), 真结算器是 settle_extended_hours.py "
                "(cron 30 6 * * 2-6; 可复跑: scripts/settle_extended_hours.py)。另 avg_capture_pct 出现 -599%/-317% = mean-of-ratios "
                "被小 mfe 行放大, 属报表口径瑕疵, 不可当判据读数。",
    },
    "eth_afterhours_exit_shadow": {
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "同上批结算 (settle_extended_hours 9/26): 盘后 hold_to_next_open n=112 "
                "avg_t0_close_ret **-6.992%** / best_holdout -2.307% / avg_mfe **-4.237%** "
                "(负 MFE = 全窗未翻绿); hold_no_stop n=2640 -0.028% / best_holdout +0.051% / "
                "mfe +2.138% / flip_rate 15.7%。⇒ 判据「净额>0 且 n≥30」**不过** "
                "(可复跑: scripts/settle_extended_hours.py; 盘后无一族净额>0; hold_to_next_open 显著为负 = 再证「盘后是惯性时段"
                "非修复时段」ah_exit_shadow 8/27 裁决)。两时段合并评估前置条件 "
                "(分布无系统性差异) 亦不成立: 盘前 tighten +0.369% vs 盘后同族为负。",
    },
    "eth_earnings_direction_path": {
        # 已读数待攒样: 尺子修好后出了真读数, 判据未过 ⇒ 维持攒样, 不到用户面前。
        "read_at": "2026-10-15", "recheck_after": "2026-10-31", "needs_user": False,
        "note": "📐 10-15 复读 (产物 112 行 / 17 distinct 日, 9/21~10-14, cron 在跑): "
                "①相位标定 112/112=100%; distinct 日 17≥15 ⇒ 「覆盖率稳定」轴已可判; "
                "②SHORT n=26(<30✗) 净均值+23.80 净中位+86.23 胜率57.7% / LONG n=12(<30✗) "
                "净均值−107.70 净中位−38.31 胜率41.7% ⇒ 判据未过 (两方向 n 不足 + LONG 净<0), "
                "维持攒样。⚠️ min_n=30 数总行数(112) vs 预注册门槛数每方向笔数 双轴错配仍在 "
                "(未擅改 min_n=改判据)。│9/26 首轮实跑 earnings_direction_check.py **失败** (FileNotFoundError: "
                "/root/.hermes/cache/scratch/replay_earnings_criterion_final.json — 脚本依赖"
                "一次性回放产物, 该目录 72h 自动清理 ⇒ eval 结构性不可重跑 = 尺子坏了, "
                "非影子断供)。产物本体健康 (cron 20 4 * * 2-6 在跑)。"
                "当时挂账「待办 = 把 eval 改读入库产物或补挂回放」。"
                "✅ **待办已于同日 (9/26 RFC-109④-D) 完成**: 新建 "
                "scripts/earnings_direction_path_eval.py 改读入库产物 "
                "data/earnings_direction_path_shadow.jsonl (可无限重跑, 只读零副作用), "
                "注册项 eval_script 已改指 + eval_on_demand=True ⇒ 挂载哨兵「悬空观察」告警 "
                "解除 (mount 告警 1→0)。原 check.py 的三个结构性问题 (scratch 72h 清理 / "
                "无 main() 保护 import 即跑 / 读的 118 条回放与本影子不是同一数据源) 见注册项 notes。"
                "📐**修好尺子后的真读数 (9/26)**: n=32 行 / 仅 4 个 distinct 日 (9/22~9/25)。"
                "①相位标定正确率 32/32=100% (gap≥+3% 10/10 SHORT, +0.5~2% 2/2 LONG, 其它 20/20 NONE) "
                "但 4 天判不了「覆盖率**稳定**」。②预注册门槛 (每方向≥30笔 且 净/笔>0): "
                "SHORT n=10 净均值+3.17% 胜率50% (净>0✓ 但 n<30✗); LONG n=2 净中位−1.30% (双✗) "
                "⇒ **判据未过, 不提案转正, 生产参数不动, 维持攒样**。"
                "⚠️ 计数轴错配已披露但未擅改 (min_n=30 数总行数 vs 预注册门槛数每方向笔数 ⇒ "
                "总行数 32≥30 被 refresh_counts 机械提升 READY, 但两方向都远未达 30)。"
                "**不改 min_n 的理由**: 新 eval 已如实分方向报 n 并明判「总行数达标≠判据达标」, "
                "判决不受错轴影响 ⇒ 改门槛属改判据=重裁, 无必要不动 (做减法); 10/10 recheck 到期会 "
                "自动推回待裁面 (overdue 机制), 届时按攒样进度再判是否需要改轴。"
                "脚本头自述结论 (r=-0.006 零相关 / 上涨占比 47% / 无 miss 票路径) 系 "
                "9/23 一次性回放, 本批未能复现, 不引为裁决证据。",
    },
    "eth_short_probe": {
        # 〔2026-10-22 重看: 10-18 已 KILLED 终态 (RFC-099 §10 探针实证完成, 产物冻结)
        #   ⇒ 闹钟结案, 不再周期重看; 重开须新预注册。〕
        "read_at": "2026-10-22", "recheck_after": "", "needs_user": False,
        "note": "✅**§10 复核已裁 (2026-10-06, 四条触发独立实测均不命中 ⇒ 续开观察, 零行为变更)**: "
                "①券源/合规: EXT 空侧探针/成交无券商拒单、无裸卖 (日志 reject 行全为 FDS/JBL "
                "『surprise STALE』数据新鲜度拒绝, 非订单拒绝; shortable 全≥1); ②止损方向: 唯一 "
                "EXT 空侧成交 US.ACN 10-01 entry=212.529 / SL=223.16 在上方=正确, exit=SL_TRAIL_LOCK "
                "+$19.44; ③尾部: EXT 空侧单日净额仅 10-01 +19.44, 无一日 ≤-$400 (连续 0 天); "
                "④观测 (按 §10.1 更正后口径=闸计数唯一面): 近 3 个日档 39/114/66 行全 >0, 未停摆。"
                "样本 n=1 不足以判『续开有效』或调门 ⇒ 按 §10.1 预注册建议续开观察, "
                "EXT_ENTRY_ALLOW_SHORT=true 维持; 下次重看 10/20。"
                "〔前读 2026-09-26: 复核窗未到不裁决, 闹钟 10/7 — 本条即该闹钟的落地〕",
    },
    "short_funnel_shadow": {
        # ✅ 2026-09-26 用户裁「2 按建议执行」⇒ 维持观测, 不在本批立 bridge 层门
        # (立门 = 改 pipeline 加门 = 生产行为变更 = L3, 须另立 DEB 预注册后再裁)。
        # needs_user=False 退出待裁面; recheck_after=2026-10-26 保留重看窗 (不用
        # no_adjudication_reason — 那会永久屏蔽裁决通道, early_dead 批同一坑)。
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "✅**已裁 (2026-09-26 用户裁「按建议执行」)**: 维持观测继续攒样; **不立 bridge 层门** — "
                "26 日同轴回算指向若立门该立在 bridge/通道候选层 (最大漏斗口 27 只, 主因 "
                "`SKIP: 非当前通道候选(bridge未提名/无方向)` ×17), 但立门 = 改 pipeline 加门 = "
                "**生产行为变更 = L3**, 须另立 DEB 预注册 (含误杀预算与对照臂) 后再裁, 本批不做。 (裁决出处: [RFC-109] commit 47f30ff74, 用户裁「按建议执行」)"
                "eval 继续只印不判; 10/26 重看窗到点自动推回待裁面 (overdue 机制)。"
                "────────────────────\n"
                "补登后首次入待裁面 (30/10 交易日, 8/16 起早已达标却从未被通知 = "
                "「注册表非 SSOT」实锤)。│**9/26 二批 (用户裁 A 案: 修轴 + 删死列) 落地后重出读数** — "
                "建 eval_script=scripts/short_funnel_shadow_eval.py (填 NO_EVAL_SCRIPT 缺口), 并按 "
                "用户 8/14 口径「历史可行直接回测, 不默认攒样」把 gz 轮转日志回算的 26 个交易日"
                "**同轴回填**进产物 (双守卫: 评级数与看空数都须与原采集值一致才回填; 行数 30→30, "
                "既有键 0 改动; 8/14-8/19 四日日志已清理不可回算 ⇒ 无新键, 消费方印「旧行无同轴键」)。"
                "同轴链 (按 symbol JOIN): DA 分析 578 → 评级看空 62 (10.7%) → **看空真出空头指令 35 "
                "(转化 56.5%)** → 真成交 19 (54.3%); 全源空头指令 98 条 / 成交 48 笔, 其中**旁路 "
                "(当日 DA 未评看空, 走扫描层 top_bearish) 54 只**。"
                "🔴**撤回并证伪本条 9/26 首批写的「断点仍在 LLM 分析层转化率 (5/21=24% 看空, "
                "3/21=14% 出指令)」**: 证伪源 = 那两个数**不同轴** — rated_bearish_n 是评级看空的"
                "**股票数**, short_instructions 是当日**全部** SELL_SHORT 指令数 (与评级无 JOIN), "
                "相除无意义 (同口径下 108/67=161% 的假「转化率」); 且它只取了 9/25 单日 (分析 8 只的"
                "小样本日)。按 symbol 同轴回算 26 日后, 看空→指令转化 **56.5% 过半**, 8/16 立项诊断"
                "「断点在 LLM 分析层转化率低」**不成立**。真正的最大漏斗口 = **评级→指令层 (掐掉 27 只)**, "
                "主因 `SKIP: 非当前通道候选(bridge未提名/无方向)` ×17, 其次 admission 价量门 ×4 "
                "(PRICE_TOO_LOW/NOTIONAL_TOO_SMALL)、sector 集中度 ×1、当日无任何指令 ×4; "
                "指令→成交层只掐掉 16 只。"
                "│**另删死列** gate_block_hints (30 天恒 0): 采集脚本用正则 `三问|逼空|催化剂时效` 数"
                "「被三问门拦掉的空头」, 实测同期 **1853 条非空 skip_reason 命中 0 条** — 生产文案里"
                "根本没这三个词 (代码中的「三问」全指用户三问 Q1/Q2/Q3 决策留痕, 不是一个门) ⇒ 门恒"
                "不触发 = 死测量。按「死门三路径全中→删门非加守卫」删除并连带清理下游 (看板段②改印"
                "同轴链 + 主因分布; 词典 conclusion 重写; 本条 description 去掉「三问gate痕迹」), "
                "替换为真实拦截原因分布 bearish_no_short_reasons。"
                "│**[只印不判] 仍待用户裁**: 判据「n≥10 交易日再裁分层 gate」已达成 (30/10), 但"
                "「立分层 gate」= 生产行为变更 (改 pipeline 加门) = L3 ⇒ eval 只出读数不判决, 须用户裁 "
                "+ 另走 DEB。读数指向: 若要立门, **该立在 bridge/通道候选层** (最大漏斗口 27 只), "
                "立错层就是第二个恒不触发的死门。另须一并裁: 旁路空头占 55% (54/98) ⇒ DA 评级只解释"
                "约四成空头产出, 判据原文假设的「LLM 分析层 → 指令」单层漏斗并不完整。"
                "关联用户 8/16 四原则「核心=真正有空头产出、禁纯单向多头」: **有产出已被证实** "
                "(26 日 48 笔空头成交), 待裁的是「要不要为那 27 只被 bridge 掐掉的看空单开门」。",
    },
    # ── 2026-09-26 补登记批 (B19/B21/B22): 判据字典有、注册表查无 ⇒ 周检/哨兵/意图守卫全盲 ──
    # 同型缺口第二次实锤 (首次 = 上条 short_funnel_shadow「8/16 起达标却从未被通知」):
    # B21 n 轴实测 276 (门 20) 早已达标, 但因不在注册表 ⇒ 一个月从未被读过。
    # 三条登记当日已实跑判据 (见各自 verdict_history), 故 read_at=登记日; 不是"没读就登记"。
    "eod_depth_gate": {
        "read_at": "2026-10-14", "recheck_after": "", "needs_user": False,
        "note": "2026-10-14 终裁 GATE_NOT_WORTH (终态, DEB-20261009 §42, 用户裁「登记」): "
                "n=9(≥8)/ΣΔ=−293.98(≤0)/miskill=5(>1), miskill 永久冻结 ⇒ verdict 不可逆, "
                "转硬闸路径正式关闭; 处置=record-only 续采 (§42.4-1), 停采另裁 ⇒ 终态不再设重看闹钟。"
                "│历史: 2026-09-26 实跑 n=2/8 ΣΔ=+28.40 误杀=1 ⇒ 判据 2/3, n 不足自动续观。",
    },
    "eth_exit_forgone_upside": {
        # ✅ 2026-09-26 用户裁 **A 案** (n 轴口径: 维持字面集合含 tighten, 不重注册不重攒) ⇒
        # 判据三门 1/3 未过线, 按 criteria 原文转 KILLED 留档为反证; 已退出待裁面。
        # recheck_after=None: 已终裁, 不再周期性推回 (若未来重启 grace window 假设须新预注册)。
        "read_at": "2026-09-26", "recheck_after": None, "needs_user": False,
        "note": "✅**已裁 (2026-09-26 用户裁 A 案)**: n 轴口径维持字面集合 (含 tighten), 不重注册不重攒; "
                "判据三门 1/3 未过线 ⇒ status 转 KILLED, 「触线即出」语义维持, 本判据留档为反证 (读数出处: 判据字典 B21, docs/ADJUDICATED-CRITERIA.md)。"
                "选 A 的依据: 真出场轴 (剔 tighten) 仅 8 笔, 稳态速率下攒到 n≥20 需 ≈336 天 (0.9 年) "
                "⇒ B 案在可见期内无法裁决; 且两口径三门结果同向 ⇒ 结论不受口径影响。"
                "────────────────────\n"
                "B21 实跑三门: ①n≥20 ✓ ②均值 −0.2249% > 0.085% ✗ ③正值率 18.2% ≥60% ✗ "
                "⇒ 判据未过线, 现行「触线即出」语义维持 (本判据留档为反证)。"
                "📐**两个 n 口径 (2026-09-26 逐行复算精确化; 原 note 曾把两口径混写一句已更正)**: "
                "计数锚 b21_exec_settled_n=276 是 **ledger 快照口径** (extended_hours_settle_latest.json "
                "ledger 分桶: tighten 269 + 真出场腿 7 = 276, 快照对同标的多次 settle 去重后); "
                "**逐行口径** (data/extended_hours_settle.jsonl 每行一笔) = 285 "
                "(tighten 277 + 真出场 8), 差 9 即去重。两口径 tighten 占比都 ≈97% "
                "(快照 97.5% / 逐行 97.2%), 均值与正值率三门结果**同向** ⇒ 结论不受口径影响。"
                "**needs_user=True 的原因不是判据待裁, 是 n 轴口径待裁**: tighten 是「收紧止损参数」"
                "不是实际出场, 却占执行类 97% ⇒ 判据文把它列进执行类集合属登记时未分辨语义。"
                "剔 tighten 后真出场子集: 逐行 n=8 (lock_in 3 / breakeven_lock 2 / SL_TRAIL_LOCK 2 / "
                "SL_BREAKEVEN 1, 均值 −13.12%, 正值率 12.5% = 1/8), ledger 分桶 n=7 (lock_in 少 1 笔 = 去重), "
                "**同样未过线且 n<20** ⇒ 剔了也提不了案。逐笔: NOK −2.22 (9/16) / NOK +0.47 (9/17) / "
                "CTNT −38.36 (9/17) / CTNT −43.89 (9/17) / NBIS −5.59 (9/17) / ACN −7.26 (9/18) / "
                "WBD −7.33 (9/18) / MU −0.77 (9/25)。"
                "须用户裁: (A) 维持字面集合 (含 tighten, n 轴虚高但结论同向) / (B) 剔 tighten 重注册 "
                "(真出场轴, 需重攒 n≥20)。本 agent 不擅自改。"
                "**攒样现实性 (支持 A)**: 真出场腿 8/1–9/25 共 56 天仅 8 笔 = 0.143 笔/天 ⇒ "
                "从 0 攒到 n=20 需 140 天, **已有 8 笔后仍需 ≈84 天 (2.8 个月)**; "
                "但 6/8 笔集中在 9/16–18 三日 (疑似单一事件批), 剔除后只剩 2 笔 = 0.036 笔/天 "
                "⇒ 再攒 12 笔需 **≈336 天 (0.9 年)** ⇒ 稳态速率下 (B) 在可见期内无法裁决。"
                "**建议 (A)** + 在判据文标注「tighten 非实际出场, n 轴偏保守 (真出场轴仅 8 笔)」, "
                "不重注册不重攒。"
                "另 94 个 t0_close_ret_pct=0 行全是 tighten (决策价=收盘价, 疑无实际出场时点"
                "可结算) 属同一口径问题, 佐证 (B) 的语义质疑成立。",
    },
    "eth_exit_quote_age": {
        "read_at": "2026-09-26", "recheck_after": "2026-10-26", "needs_user": False,
        "note": "B22 实跑 n=6/20, 分桶 {>120s: 0, <=30s: 6} ⇒ 落判据分支②方向(维持现状不加"
                "龄门), n 不足自动续观。**攒样预期**: 十天仅 6 行 ⇒ 到 n=20 约需一月, 且分支①"
                "要求「>120s 的执行行≥1」而 >120s 迄今 100% 落在不执行的 hold_no_stop 行 ⇒ "
                "本项可能长期停在分支②(留档反证), 这是判据文写明的预期结局之一, 不是断供。"
                "本条是本 agent 上一轮自己登记的判据, 同型欠账 (登判据字典时漏同步注册表)。",
    },
    # ── 2026-10-03 清理批: 待裁面「已读判据」登记 (把 7 项机械读数从 needs_user 面摘下,
    #    全部为当日实跑/既有产物读数, 判据一字未改; 改判据=重新裁决) ──────────────────
    "ue_tier_gate": {
        "read_at": "2026-10-19", "recheck_after": "", "needs_user": False,
        "note": "2026-10-19 用户令按建议执行 ⇒ 退役 (KILLED): 写者仅挂 UE inject, "
                "10/18 DA 主链改造后路径不再运行; 数据止于 10/05; 被拦 fwd n=4<30; "
                "eval cron 已摘; 数据保留; 重开=新预注册。",
    },
    "ue_quote_freshness_shadow": {
        # 〔2026-10-22 重看: live n=28/10; 10-09 verdict=NOT_READY (genuine 龄 P95=1283.9s ≫
        #   90s 阈值, 激活会系统性误杀) 维持不激活; 阈值改动须重新预注册, 结论不变。〕
        "read_at": "2026-10-22", "recheck_after": "2026-11-21", "needs_user": False,
        "note": "10/03 实跑: 有效 14/10 日, genuine 龄 P95=1283.9s ≫ 阈值 90s (14 倍) ⇒ "
                "verdict=NOT_READY — 现值激活会系统性误杀 (超阈 25 会新拒 15), 维持不激活。"
                "预注册纪律: 不得事后改阈值重算; 盘前专属阈值须重新预注册。",
    },
    "fee_gate_shadow": {
        # 〔2026-10-22 重看: live n=161/30; 方向对 (被拦更差) 但子样本 2-4 笔太小;
        #   注记「攒样至 10/11 双窗口回放终裁窗」已过期未裁 ⇒ 转 needs_user。
        #   ⚠️并陈 10-22 新证据: 「$3-5k 名义桶费/毛 2.16」结论已衰减 (9/1 起复测 1.03,
        #   近 11 日该桶净 +$129.72) ⇒ 名义地板类子门的靶面已变, 裁决时须用新口径复算。〕
        "read_at": "2026-10-22", "recheck_after": "2026-10-29", "needs_user": True,
        "note": "fee_gate_shadow_latest.json (cron 45 6 产物): baseline n=64, 期望/笔 −$2.48; "
                "各门 blocked 子集: price<$1 n=2 / 名义<$4k n=2 (exp −$41.6) / <$5k n=4 "
                "(exp −$20.5) ⇒ 方向对 (被拦更差) 但子样本 2-4 笔太小, 不构成转正证据, "
                "继续攒样至 10/11 双窗口回放终裁窗。",
    },
    "trail_lock_pyramid": {
        "read_at": "2026-10-22", "recheck_after": "", "needs_user": False,
        "note": "✅终裁 (2026-10-22 用户裁「按判据裁 FAIL」, 全链路复查 P1-3): n 门达标 "
                "(n_executable=86≥40, 产物 10-21 18:41 ET) 但判据 1/4 — 腿净中位 −$0.32✗ / "
                "Σ腿净 −$6.60✗ / 回吐门✓ ⇒ 落 §七「否则」分支: 维持现状结题, 不上 sim, 停采 "
                "(pyramid_shadow_enabled=false + settle cron 注释退役)。「测不出≠证伪」条款不挡 "
                "FAIL: 实测 Σ 符号为负, 属方向不成立而非功效不足。│历史: 10/03 实跑 "
                "armed=31/settled=25/executable=22/40✗, 中位−0.28✗ Σ−$13.63✗ 回吐✓ (1/4), "
                "t=−2.312 MDE80=4.13 ⇒ KEEP_SAMPLING (n 未满, 攒满再机械判)。",
    },
    "eth_rth_fade": {
        # 2026-10-22 首次登记读数 (样本达标未裁欠账清理, 全链路复查 R4):
        "read_at": "2026-10-22", "recheck_after": "2026-10-29", "needs_user": True,
        "note": "10-22 实跑 rth_fade_graduation_check.py: 样本门①过 (16 RTH 日/39 笔 ≥15日/30笔), "
                "但判据 ②净/笔 −$15.17✗ (>0) ③胜率 54%✗ (≥55%) ④留一口 −$34.94✗ (≥0) ⇒ 1/4; "
                "脚本 verdict=继续攒样。**处置待用户裁**: 继续攒样 (现口径) vs 对照 RFC-098 终止条款 "
                "判 FAIL — 负向读数已在足样本下成立, 继续攒样的边际信息价值存疑。",
    },
    "overnight_known_event_flag": {
        # 2026-10-22 首次登记读数 (样本达标未裁欠账清理, 全链路复查 R4):
        "read_at": "2026-10-22", "recheck_after": "2026-11-05", "needs_user": True,
        "note": "⚠️口径错配修正: 注册表 n=23 是标记行数, **判据轴=采样日数** — 10-22 实跑 "
                "--report: 4/20 (flagged 0/≥8, unflagged 4) 远未达。且 09-25 起 19 个计划断供日 "
                "⇒ 采集处于饿死状态。**处置待用户裁**: 修计划供给 vs 退役。",
    },
    "rfc104_eth_event_plan": {
        "read_at": "2026-10-15", "recheck_after": "", "needs_user": False,
        "note": "2026-10-15 终裁结案: 用户裁「判阶段0 FAIL 关停」— 覆盖率 15 日从未达标 + "
                "条数带上界持续被破 ⇒ PHASE0_FAIL (终态); 全 8 行 cron 已注释, 注册表 KILLED, "
                "复活须重新预注册。│graduation_check (10-15 10:08 自动跑, cron 8 10 * * 2-6 — 9/26「计数锚读陈旧快照」"
                "缺陷已由该 cron 修复): ledger 33 行/15 交易日 (PASS 14/FAIL 11/NO_DATA 8) ⇒ "
                "①交易日数 15≥7 ✅ ②覆盖率 100% 仅 6/15 日 ❌ (9 日漏票: 09-24/28/30, 10-01/06/07/08/09/13) "
                "③计划条数带 [0,44] ❌ (09-28 两窗 45/49, 均值 47.0 ⇒ 附注「计划层可能正在退化成盲扫」) "
                "⇒ verdict=NOT_READY, 阶段1 不立项。另: 注册表 "
                "eval_script 原挂错 (eth_event_plan.py=攒计划器), 已改指 "
                "eth_event_plan_graduation_check.py (2026-10-15 审计 P1-3)。",
    },
    "ext_entry_exec_price_shadow": {
        # 〔2026-10-22 重看: 10-18 已 KILLED 终态 (减法批退役) ⇒ 闹钟结案, 不再周期重看。〕
        "read_at": "2026-10-22", "recheck_after": "", "needs_user": False,
        "note": "10/03 按预注册四点直读 jsonl+DB (n=546 轮/5 日, 门 ≥60 行/≥3 日 ✓; "
                "完整口径与 SQL 见 docs/decisions/REVIEW-20261003-shadow-cleanup.md): "
                "①中位 candidates_n=46 / current_price_pool_n=12 / shadow_pool_n=2; "
                "②$5→$50 候选池缩减中位 73.9% (均值 72.7%); ③窗口内 EXT 实际成交 1 笔 "
                "(ACN 10/01), entry<$50 占比 0/1; ④shadow 价池 ∩ 实际成交 = ACN 10/01 (1 命中, "
                "即现行唯一成交不会被 $50 门槛排除)。首读完成但窗口仅 5 日 ⇒ 续观至判据窗/死线 11/30。",
    },
}


def adjudication_state(item: "ShadowItem") -> dict:
    """该事项的裁决面状态 (供通知/呈现消费; 只读, 不改状态机)。

    返回 {"read_at", "recheck_after", "needs_user", "adjudicated": bool, "overdue": bool}。
    未登记 ⇒ adjudicated=False (真正没读过判据 = 待裁面)。
    """
    from datetime import date

    rec = ADJUDICATED.get(item.id)
    if not rec:
        return {"read_at": "", "recheck_after": "", "needs_user": False,
                "adjudicated": False, "overdue": False}
    due = str(rec.get("recheck_after") or "")
    overdue = False
    if due:
        try:
            overdue = date.fromisoformat(due) <= date.today()
        except ValueError:
            overdue = False
    return {"read_at": str(rec.get("read_at") or ""), "recheck_after": due,
            "needs_user": bool(rec.get("needs_user")), "adjudicated": True, "overdue": overdue}


REGISTRY: list[ShadowItem] = [
    # ── Phase 1 (2026-08-23) ──
    ShadowItem(
        id="p1_1_vwap_slope",
        name="VWAP 斜率证伪门",
        status=ShadowStatus.GRADUATED,
        description="入场前 5min 量加权价逆方向 → 动量反驳。全源生产拦截 (reentry+UE 2026-08-23 起, DA 2026-09-12 RFC-082 起)。",
        data_path="data/trigger_miss_telemetry.jsonl (reason=falsification_vwap_slope)",
        config_key="playbook.execution_parameters.falsification_gate_vwap_slope_enabled",
        min_n=30,
        graduation_criteria="reentry+UE 组 n≥30 + 日聚类 t≤-2 + DA 对比组方向一致 → 翻 da_enforce=true",
        eval_script="scripts/falsification_gate_weekly_review.py",
        created="2026-08-23",
        last_evaluated="2026-09-12",
        verdict_history=[
            {"date": "2026-09-12", "verdict": "GRADUATED_BY_USER_OVERRIDE",
             "evidence": "RFC-082 用户裁「已过大量收集数据阶段, 该出手出手, 以盈利和降低亏损为目标」— "
                         "知情推翻预注册判据 (reentry+UE 真被拦 n=2 < 30, t=-1.303 未达 -2)。"
                         "转正依据: DA 对比组 8/27-9/11 共 13 行 (12 行 join 真实成交), 若当时拦下省 $735; "
                         "8/28 单日 5 行全中省 $839 (AFRM/RBRK/GAP = 当日 SL_INITIAL+隔夜缺口出血主力); "
                         "与近4周最大出血点 SL_INITIAL 入场即逆 (18笔 -$3,069, DA占12笔 -$2,298) 同模式; "
                         "强制源实测拦截 60% (3/5) 为延迟成交 = 门形态偏延迟器, 期望值打折已披露。"
                         "da_enforce false→true + 重启。回滚: 改回 false + 重启, 本条 revert OBSERVING。"},
        ],
        notes="按源分流历史: reentry+UE 拦截 (2026-08-23), DA 对比组记录不拦; E2 n=136 方向极强。"
              "2026-09-12 起全源拦截; 周检脚本继续按全源计算真被拦组 (DA 行今后进入强制拦截桶, "
              "对比组读数自然停增 — 判读时以 2026-09-12 为 DA enforce 分界)。",
    ),
    ShadowItem(
        id="p1_3_vol_p50",
        name="量能门 p50 曲线",
        status=ShadowStatus.KILLED,
        description="chase 路径分母改历史同分钟 p50。reentry+UE 启用, DA 走线性(对比组)。",
        data_path="data/vol_gate_da_p50_shadow.jsonl",
        config_key="playbook.execution_parameters.volume_gate_vwap_relative",
        min_n=30,
        graduation_criteria="reentry+UE 组 n≥30 chase + 重标定阈值 + DA 对比组方向一致 → 翻 da_enforce=true",
        eval_script="scripts/phase1_gate_backtest.py",
        created="2026-08-23",
        last_evaluated="2026-09-01",
        verdict_history=[{"date": "2026-09-01", "verdict": "KILLED",
                         "evidence": "挂载核对哨兵首跑捕获: eval 从未挂载 + 数据停滞 (8/23-8/28 仅 3 行, "
                                     "8/28 后断流) → 触发条件在生产中实际不存在。用户裁'按建议执行'(选项1裁杀)。"
                                     "在跑行为不受影响: volume_gate_vwap_relative=true 系 reentry+UE 侧 "
                                     "既有行为, 本裁杀仅停观察跟踪; DA 对比组写盘随 da_enforce 现状。"}],
        notes="按源分流: reentry+UE 走 p50, DA 走线性; 核心桶 [0.6,0.8) WR 20% 方向强。"
              "2026-09-01 裁杀: 5 天 3 样本=触发近乎不存在, 攒样无意义。"
              "✅**已裁维持不停 (2026-09-26 用户裁「5 不停」, 依据 RFC-109⑤ 盘点: 停采须改生产码/拆写方, 而负载微小 ⇒ 风险>收益)。** 原盘点 (L3 提案, 未执行): 影子发射块 pipeline:5757 "
              "`if _vol_use_p50 is False:` **无独立开关** (该条件是生产分流本身, 非影子开关) "
              "⇒ 停采须改 pipeline.py 加 shadow-only 键 + systemctl restart; "
              "⚠️ config_key=volume_gate_vwap_relative 是**生产量能门**(现true), 照它停采=关生产闸。"
              "负载实测 172K/624行, 近7日+118 ⇒ 收益微小, 不值当改生产码。",
    ),
    ShadowItem(
        id="p1_1_capital_flow",
        name="资金流背离 record-only",
        status=ShadowStatus.KILLED,
        description="大单净流逆方向 → 背离。回放四关门全不过(p=0.85), 纯 record-only。",
        data_path="data/falsification_gate_shadow.jsonl",
        config_key="playbook.execution_parameters.falsification_gate_capital_flow_shadow",
        min_n=30,
        graduation_criteria="同四关门判据 (capital_flow_replay_verdict.json): inflow/outflow t≤-2 + IS-OOS 同号 + placebo p<0.05",
        eval_script=None,
        created="2026-08-23",
        last_evaluated="2026-09-21",
        # 2026-09-26 用户裁「现在重启一次性解决」⇒ 停采已执行 (详见 notes 与台账 G10)。
        # 注意: collection_retired 对本项**不是**停采的执行机制 (pipeline.py 不读该字段), 真停采
        # 靠 config 键 falsification_gate_capital_flow_shadow=false; 这里设 True 的作用是让看板
        # (shadow_board.py:660,674) 给本条打退役标 ⇒ 免得「已停采却仍印历史数据」误导读者。
        collection_retired=True,
        verdict_history=[{"date": "2026-09-21", "verdict": "KILLED",
                          "evidence": "清账批 (RFC-092): 判据产物 data/capital_flow_replay_verdict.json "
                                      "(2026-08-22) 四关门 all_gates_pass=False — inflow t=-0.48 / "
                                      "outflow t=-0.71 均不显著, IS/OOS 异号, placebo p=0.85; "
                                      "且用户已裁'不得拦单' ⇒ 该假设已判死, 注册表未同步。归档退役。"}],
        notes="capital_flow_replay_verdict: 四关全不过, 用户裁决不得拦单。"
              "2026-09-21 清账: 已裁未归档 → 退役, 退出待裁队列。"
              "✅**停采已执行 2026-09-26** (RFC-109⑤/G10, 用户裁「现在重启一次性解决」): "
              "`playbook.execution_parameters.falsification_gate_capital_flow_shadow` true→false, "
              "`systemctl restart ftseek-us` 于 16:17:27 CST 完成 (新 PID 2995319, active/running, "
              "journalctl 16:17 起 **0 条** ERROR/Traceback, 订阅循环+watchdog+ManualGuard 均恢复, "
              "16:18:36 钉钉心跳成功)。config 无第二份、无 .env 覆盖 ⇒ 启动时读到的就是 false。"
              "生效链核验: 本项 9/21 已判 KILLED (四关门 p=0.85) 且用户已裁不得拦单 ⇒ 判据已判死, "
              "「攒样后重裁是否转生产」的预期已终结, 继续采集无裁决价值; 裁决证据在 "
              "data/capital_flow_replay_verdict.json (2026-08-22), **不依赖**持续采集 ⇒ 停采不断证据。"
              "零交易行为变更: 该键**唯一读点** pipeline:6274 只包 shadow 写盘 (注释明写「不得拦单」), "
              "产物 falsification_gate_shadow.jsonl **零读方** (intraday_reentry:742 的资金背离门读 "
              "data/futu_capital_flow.jsonl 分钟级原料, **不是**本 jsonl ⇒ fail-open 陷阱属 "
              "futu_channel_capital_flow, 不适用本条)。负载省下 416K/1594行 (近7日+412)。"
              "⚠️**尚待观测确认**: 停采日 9/26 是周六 (ET 04:1x 非交易时段), 产物最后写入停在 "
              "9/25 21:46 ⇒「重启后无新增行」与非交易日不可区分 ⇒ 须下周一 (9/28 ET) 开盘后核 "
              "产物行数仍为 1594 才算行为面确认 (回滚: 键改回 true + 重启)。"
              "│其余 4 条终态影子**未停** (建议维持): p1_3_vol_p50 / mfe_stall_exit 须改生产码 "
              "(无 shadow-only 开关); pullback_limit_shadow 两个语义共用写方 (第二写方 "
              "playbook_service._append_routed_shadow 是活跃的 archetype 路由遥测, 关开关只停一半 "
              "且断下游按 type 取行的重放归因); overnight_kill_criterion 的 data_path 指报表输出"
              "兼注册表计数锚 ⇒ 停采=断证据非减负载 (近7日 +0 行, 收益≈0)。"
              "陷阱划清: 原审计点名的两个生产键确认禁碰且已复核未动 "
              "(volume_gate_vwap_relative=True 生产量能门 / mfe_stall_exit_enabled=False 生产退出腿)。",
    ),

    # ── 富途通道影子 (采集 8/12 已上线, 2026-08-29 补登记 — 未登记=无人跟踪) ──
    ShadowItem(
        id="futu_channel_capital_flow",
        name="富途真资金流 (增量IC)",
        status=ShadowStatus.OBSERVING,
        description="通道 B: get_capital_flow 分钟级大小单净流入 (真主力资金, 非量价合成代理)。"
                    "8/12 已上线采集, 8/29 复核发现未登记补录。8/24 初评: "
                    "rank_ic_marginal=0.1188, t=1.49, n_pairs=157 → WEAK_EVIDENCE (未显著)。",
        data_path="data/capital_flow_daily.jsonl",
        config_key="futu_channels.shadow_collect_enabled",
        min_n=540,
        graduation_criteria="DEB-20260812 §五预注册: 增量 rank-IC (OFI 基线上) 双窗口 OOS ≥40% "
                            "或 ρ≥0.15 + 非空率 ≥95% + n≥30 交易日。评估: scripts/futu_channel_shadow_eval.py",
        eval_script="scripts/futu_channel_shadow_eval.py",
        created="2026-08-12",
        last_evaluated="2026-08-29",
        # eval=裁点手动触发 (runner ops/hermes/futu_channel_adjudication_run.py 自身无
        # cron, 8/24 与 8/29 两轮均为手动) — 2026-09-01 复查收口显式定性, 不再靠
        # 源码文本碰巧豁免挂载核对; 样本到量由周检 READY 通知触发裁决。
        eval_on_demand=True,
        verdict_history=[
            {"date": "2026-08-24", "verdict": "WEAK_EVIDENCE",
             "evidence": "ic=0.1188 t=1.49 n_pairs=157 oos同号(0.194/0.086) 未显著"},
            {"date": "2026-08-29", "verdict": "FAIL_INTERIM",
             "evidence": "ic=0.0511 t=0.595 n_pairs=137 较8/24衰减; OOS双窗异号(+0.134/-0.051) "
                         "→ 毕业判据FAIL; 未达min_n=540继续攒样, 但若下次IC继续衰减/OOS仍异号 → 建议提前裁KILLED"}],
        collection_retired=False,
        notes="行数口径: ~18标的×30交易日≈540行 (2026-09-20 活体核查实测 capital_flow_daily.jsonl=483行, "
              "原记(当前258)已过期); 分钟级原料 data/futu_capital_flow.jsonl; "
              "OFI基线已就位(7817行, overlap 91对, flow_ofi_rank_corr=0.089 低共线) "
              "⚠️ 2026-09-20 活体核查补注 (防'零消费→KILL'误裁): 原始 jsonl 有**生产消费方** — "
              "app/playbook/intraday_reentry.py 盘中资金背离门读它取当日大单净流方向 "
              "(_capital_flow_today_index :742/:787, 判定 :441-446, 调用 :882/:965); 另有 "
              "gate_liveness_probe.py:145 / reentry_gate_effect_tracker.py:44 / "
              "capital_flow_daily_aggregate.py / futu_channel_shadow_eval.py 消费。⇒ 本条目判据 FAIL "
              "只针对**日级聚合增量 IC**, 与盘中背离门不是同一口径 (门的独立影子读数 "
              "reentry_gate_effect_report.json: would_block n=9 avg −6.63 vs pass n=44 avg −17.77), "
              "两者禁互相冒充; KILL 采集会断该门的料 (fail-open 后退化为无门)。"
              "🔒 2026-09-21 (用户裁'无效就停' 时复核, 差点误停): **决定不停采** — "
              "collection_retired 保持 False, 新增独立开关 futu_channels.capital_flow_collect_enabled=true。"
              "理由: (a) 原料有生产消费者 (上条); (b) 判 FAIL 的是日级聚合增量**研究判据**, "
              "判据退役≠采集退役, 二者禁互相反推 (本字段与 status 刻意分开, 见 dataclass 注释); "
              "(c) 39MB 是累计体积而非单日成本 (实测 1.15–1.88MB/交易日, 消费侧已做尾部 4MB 增量读)。"
              "要动的是**这条判据** (8/29 FAIL_INTERIM 后未再裁决) 与**盘中门自身**的方向问题, 不是采集。",
    ),
    ShadowItem(
        id="futu_channel_deriv_unusual",
        name="富途期权大单异动 (领先假说)",
        status=ShadowStatus.KILLED,
        description="通道 A: get_derivative_unusual 期权大单事件 → 事件后窗口超额收益配对基线 "
                    "(Cremers-Weinbaum 知情资金先进期权假说)。8/12 已上线, 8/29 补登记。"
                    "8/24 初评: 超额均收益 -0.006%, 超额胜率 47.5% — 暂无领先证据。",
        data_path="data/futu_deriv_unusual.jsonl",
        config_key=None,
        min_n=3000,
        current_n=4634,
        graduation_criteria="DEB-20260812 §五预注册: 事件后窗口收益 > 同票无事件基线 (配对) "
                            "+ 非空率 ≥95% + n≥30。评估: scripts/futu_channel_shadow_eval.py",
        eval_script="scripts/futu_channel_shadow_eval.py",
        created="2026-08-12",
        last_evaluated="2026-08-29",
        verdict_history=[
            {"date": "2026-08-24", "verdict": "NO_EDGE_YET",
             "evidence": "excess_mean_ret=-0.00006 excess_win_rate=0.475 n_paired=3207"},
            {"date": "2026-08-29", "verdict": "NO_EDGE_CONFIRMED",
             "evidence": "n_paired=3556 excess_mean_ret=-0.00014 excess_win_rate=0.478 "
                         "(post+0.008% vs baseline+0.022%)。连续两次独立评估超额均为负"},
            {"date": "2026-08-29", "verdict": "KILLED",
             "evidence": "用户裁'3批': 预注册判据不达且两连否 → 停采 (deriv_collect_enabled=false), "
                         "历史数据 4634 事件保留备查, 通道B/C/D 不受影响"}],
        notes="8/29 用户裁决 KILLED, 停采生效; 2026-09-16 删除采集实现与开关, "
              "历史 data/futu_deriv_unusual.jsonl 保留",
    ),
    ShadowItem(
        id="futu_channel_ofi_baseline",
        name="富途 OFI 订单流失衡基线 (通道 C)",
        status=ShadowStatus.OBSERVING,
        description="通道 C (8/21 上线, 8/29 补登记): get_market_snapshot level-1 盘口快照 "
                    "{bid1/ask1 量价} → 评估侧对相邻快照算 Cont-Kukanov-Stoikov 增量 ΔOFI。"
                    "用途: capital_flow 增量 IC 的 OFI 基线口径 (预注册纪律'禁裸边际'解法)。"
                    "独立开关可单独回滚。",
        data_path="data/futu_ofi_baseline.jsonl",
        config_key="futu_channels.ofi_collect_enabled",
        min_n=600,
        graduation_criteria="OFI 增量与后续窗口收益的 IC 显著 (基线口径) 才毕业; "
                            "评估: scripts/futu_channel_shadow_eval.py (与通道 B 联动)。",
        eval_script="scripts/futu_channel_shadow_eval.py",
        created="2026-08-21",
        # eval 与通道B联动=裁点手动触发 (同 futu_channel_capital_flow, 2026-09-01 定性)
        eval_on_demand=True,
        notes="count_mode=distinct:ts_et — 按快照时刻去重计数 (每轮采集多标的共享同一 ts); "
              "8/21-8/28 已攒 428 快照 / 7817 行",
        count_mode="distinct:ts_et",
    ),
    ShadowItem(
        id="futu_channel_top_movers",
        name="富途全市场盘中异动榜 (通道 E)",
        status=ShadowStatus.SUPERSEDED,
        description="通道 E (8/29 新增, 能力利用率审计用户裁'全做'): get_top_movers_rank(Market.US) "
                    "全市场实时异动榜快照 (~1832标的, 每轮1行含Top100涨幅/换手/量比)。"
                    "此前系统盘中无异动发现通道 — 影子先行攒样评估 "
                    "'盘中异动 → 后续窗口收益' 有无信号, 不进门禁。",
        data_path="data/shadow/top_movers_eval_latest.json",
        config_key="futu_channels.movers_shadow_enabled",
        min_n=540,
        graduation_criteria="预注册: 异动榜Top100入选 → 后续30min/日内窗口超额收益配对基线, "
                            "IC 显著才评估接候选池发现通道; 评估脚本待建 (先攒样)。",
        eval_script="scripts/top_movers_eval.py",
        created="2026-08-29",
        last_evaluated="2026-09-21",
        intent="chase_high_ban",
        cross_ref=("kr3_direction_runup_gate",),
        verdict_history=[{"date": "2026-09-21", "verdict": "IC_SIGNIFICANT_但方向为反 → 呈裁 (不自动毕业)",
                          "evidence": "清账批 (RFC-092) 建 scripts/top_movers_eval.py 并实跑 (bar 配对不可行: "
                                      "replay_bars 对 (mover,day) 覆盖率仅 2%=87/5372, 该目录只对系统看过的标的落盘; "
                                      "改用快照自足口径: 入场=当日首次入榜的 change_pct, 结局=同日末次观测, "
                                      "fwd=(1+c_exit/100)/(1+c_entry/100)-1)。14 个 ET 会话 3291 对: "
                                      "**逐日 IC 均值 -0.4435, t=-13.18, MDE±0.094 (可分辨), 14/14 天同号**; "
                                      "剔除'change_pct 全程未更新'冻结配对 (11%) 后仍 -0.422, t=-10.74; "
                                      "时段切片 ≤10:00 t=-8.4 / 10:00-12:00 t=-17.1 / >12:00 t=-6.0 全同向。"
                                      "榜内 rank 对照: top10 fwd -0.19% vs 11-50 +0.52% vs 51-100 +0.73% (单调递减)。"
                                      "⇒ 判据字面达标 (IC 显著), 但**方向与'追涨幅榜发现候选'用途相反**: 榜首续跌、榜尾续涨。"
                                      "⚠️ 选择偏差: 只有仍在榜的标的被再次观测 (出榜样本截断); 窗口仅 14 会话单 regime。"
                                      "处置建议 (待用户裁): ①作'追高排除/冷却'信号 ②反向用作回调入场候选池 "
                                      "③并入既有追高族 (同轴, 勿新增硬门) — 均需另立预注册, 本项不自动生效。"},
                         {"date": "2026-09-21b", "verdict": "①被实测否决 → 改判③ (同轴并入, 不新增)",
                          "evidence": "用户裁'继续'后落地①的可量口径 (scripts/top_movers_trade_join.py): "
                                      "把榜单与已成交笔对齐 (入场前最后一次榜观测的 rank, 结局=pnl_net 实盘口径)。"
                                      "join 上 130/864 笔 (榜窗 8/29-9/19 仅 18 会话)。分桶: **rank<=10 n=11 笔均 +15.20 "
                                      "(WR .636)** | 11-50 n=7 +18.46 | 51-100 n=16 -0.32 | 入场时无榜观测 n=96 -4.03。"
                                      "⇒ ①的反事实**不成立**: '冷却榜前10' 会躲掉 **+167.21 净盈利** (盈利笔 Σ+611.80 "
                                      "vs 亏损笔 Σ-444.59), 日聚类差 +10.55 t=0.263 MDE±112 不可分辨, 且 n=11<30 "
                                      "(§三.5 样本门未达)。**关键结构发现: top10 的 11 笔中 10 笔与 kr3 would_block 同批** "
                                      "⇒ ①≡kr3 的同一假设, 落地即重复; 且榜级 IC(-0.44) 与笔级(+15.2) 符号相反, "
                                      "说明'追高吃亏'在**已成交笔**上不成立 (与 kr3 自身'LONG blocked 胜率 73%、"
                                      "尾部支配'一致)。**处置: 做减法 — 不新增门/影子, 轴归 kr3 (③)**; "
                                     "本项只保留其特有面 (榜内横截面 IC, kr3 不覆盖) 继续观测。"},
                         {"date": "2026-10-03", "verdict": "SUPERSEDED (③并入 kr3 轴, 采集保留)",
                          "evidence": "9/30 闹钟到期后, 用户确认执行 9/21b 建议③。判面: "
                                      "不新增门/影子/反向池, 笔级追高假设归 "
                                      "kr3_direction_runup_gate 轴; 本项仅保留特有横截面 IC "
                                      "作解读佐证。采集开关继续 true (9/21 用户裁继续采, "
                                      "RFC-092 §9.1/§10.3 与 config 注释), 停采须另行裁决。"}],
        notes="盘中每轮(300s节流)1行快照, ~78行/交易日; min_n=540≈7个交易日; "
              "可量口径二: scripts/top_movers_trade_join.py (榜单 × 已成交笔, 用于评估'冷却'类用法); "
              "**同轴声明 (RFC-092b §9)**: 本榜'追高排除/冷却'用法与 kr3 测同一批笔 (rank≤10 的 11 笔中 10 笔落在 "
              "kr3 would_block 面内) ⇒ 不得独立转正, 裁决路径并入 kr3; 本项特有待裁面 = 榜内横截面 IC (kr3 不覆盖); "
              "与盘前榜(premarket_enrichment futu_premarket_rank)互补: 盘前榜接决策上下文, 本通道纯影子",
    ),
    ShadowItem(
        id="futu_channel_iv_hv",
        name="IV/HV 前瞻波动率影子",
        status=ShadowStatus.OBSERVING,
        description="通道 D (8/29 新增, 用户裁'4也做'): get_option_underlying_his_volatility "
                    "股票级 IV (前瞻) + HV 250 日序列, 无需期权合约。用途: 影子攒样验证 "
                    "IV/HV 在已落地 atr_pct vol_buckets 上有无桶内残存效应 (前瞻补后视)。"
                    "AFTER_HOURS 每日1次, 按 (symbol, time) 去重防轮询放大。",
        data_path="data/shadow/iv_hv_residual_latest.json",
        config_key="futu_channels.iv_collect_enabled",
        min_n=30,
        graduation_criteria="残存效应分析 (基线纪律: 在已落地 atr_pct 分桶上算残存, 不另起炉灶): "
                            "IV/HV 于交易日 join trade_records, 桶内增量显著才毕业。"
                            "首日回补 250 日历史 → 历史残存效应立即可跑。",
        eval_script="scripts/iv_hv_residual_eval.py",
        # eval_on_demand=True (2026-09-26 RFC-109⑤收口): 采集已于 9/21 真停
        #   (iv_collect_enabled=false, 产物 futu_iv_hv_shadow.jsonl 最后写入 9/19 = 停更吻合),
        #   eval 是**按需一次性重跑**脚本 (10/21 重看裁决时才跑), 本就不该有 cron 挂载 ⇒
        #   check_mounting 报的「悬空观察」是**假告警**, 非真断供。与 earnings_direction_path
        #   (eval 结构性崩、告警准确指向待办) 性质不同, 故本条豁免、那条**不加**豁免。
        eval_on_demand=True,
        stale_expected=True,
        created="2026-08-29",
        last_evaluated="2026-09-21",
        verdict_history=[{"date": "2026-09-21", "verdict": "NO_RESIDUAL (判据未达, 效应不可分辨)",
                          "evidence": "清账批 (RFC-092) 建 scripts/iv_hv_residual_eval.py 并实跑: "
                                      "落桶用已落地 playbook_horizon.yaml short_horizon.vol_buckets 边界, "
                                      "atr_pct=metadata.atr/entry_price, 结局=pnl_net; 实盘已结算 473 笔 "
                                      "join 上 317 (67%)。桶内 IV/HV≥1 组 vs 其余: low n=7 "
                                      "(INSUFFICIENT_N) | medium n=41 HI +$4.66 / LO -$31.08 → pooled 差 "
                                      "+$35.74 但 **日聚类 t=2.029 且 MDE±52.4 > 效应 ⇒ 不可分辨** | "
                                      "high n=269 差 +$4.68 t=-0.026 MDE±23.8 ⇒ NO_RESIDUAL | extreme n=0。"
                                      "任何桶均未达'桶内增量显著' ⇒ 判据未达; 计数锚改指 latest.json。"}],
        notes="🔒 2026-09-21 停采 (用户裁'无效就停'): futu_channels.iv_collect_enabled=false — 已注册的假设"
              " (桶内残存效应) 结论为无残存效应, 且产物除本条目 eval 外无任何消费者 ⇒ 继续采只是继续长文件。"
              " 数据保留 (32,007 行) 不删; 登记状态仍 OBSERVING (终态留给 10/21 重看裁决)。"
              " 重开 = 新假设 + 新预注册判据 + 翻开关 (留痕), 不许只翻开关。"
              "count_mode=distinct:collected_at — 首日回补 250 日历史 (~5000 行) 只算 1 个攒样日, "
              "回补行保留供历史残存效应分析; min_n=30 攒样日",
        count_mode="distinct:collected_at",
    ),

    # ── RFC-123 阶段0 双锚影子 (DEB-20261007 F1, 2026-10-07 用户裁「按建议执行」) ──
    ShadowItem(
        id="entry_anchor_dual",
        name="入场锚双轨影子 (生成锚 vs 执行锚, RFC-123 阶段0)",
        status=ShadowStatus.OBSERVING,
        description="RFC-123 阶段0: entry_price 一列两义 (生成锚被 reprice 逐轮覆盖为执行锚, "
                    "实测 66.4% 指令已改写, 偏差中位 1.01%/p90 3.65%, E18)。写方 pipeline.py "
                    "_emit_entry_shadow 在 ETS 行加嵌套 dual_anchor 块 (生成锚=planned_entry→"
                    "band沿反推→missing 三级, 绝不回退 DB entry_price; + 生成锚反事实触发几何)。"
                    "record-only 零交易行为变更; 量化分歧面/missing率/分歧触发分布, 供阶段1 "
                    "(锚持久化分离) 裁决。missing 单列一桶不计入分歧率分子分母 (DEB §13 步3)。",
        data_path="data/entry_timing_shadow.jsonl (dual_anchor 块, anchor_recorded=True 行)",
        config_key="playbook.execution_parameters.entry_anchor_dual_shadow_enabled",
        min_n=300,
        count_mode="where:anchor_recorded=True",
        graduation_criteria="预注册见 RFC-123 §预注册判据: 影子期≥10个交易日 ∧ ①生成锚缺失率<1% "
                            "②双锚触发判定分歧率≥5%且分歧非纯数据错误 ③分歧样本按生成锚判定的"
                            "触发质量(含费PnL)不低于现行锚 ④现行护栏触顶频率无异常上升 → "
                            "另行裁决阶段1 (行为分离); 读数后不得调整入场带/确认位/触发阈值。"
                            "(n 口径 = anchor_recorded 行数 count_mode=where:anchor_recorded=True, "
                            "300 = 按 ETS 日均 ~150 行×2 交易日余量; 判据①-④的比率口径以指令×日去重)",
        eval_script="scripts/entry_anchor_dual_eval.py",
        eval_on_demand=True,
        no_adjudication_reason="PERIOD_GATE: 8/10 交易日 (截至 2026-10-16); 只读读数已建, 10/20 后重跑; 质量腿需生成锚反事实回放.",
        created="2026-10-07",
        verdict_history=[
            {"date": "2026-10-07", "verdict": "PHASE0_SHADOW_STARTED",
             "evidence": "DEB-20261007 F1 用户裁「按建议执行」落地: pipeline.py 模块级纯函数 "
                         "_resolve_generated_anchor/_anchor_trigger_geometry + _emit_entry_shadow "
                         "dual_anchor 块 + YAML 开关 entry_anchor_dual_shadow_enabled=true。"
                         "立起动机含自曝#11: 污染列已 3 次毒化本 DEB 分析 (E12/E13 伪影)。"},
        ],
        notes="阶段0=只埋点记录分歧, 判定仍完全用现行 effective_anchor (RFC-123 原文)。"
              "eval_script 待攒样后补 (阶段1 裁决前须先写结算器, 防「有入口无出口」— 台账 :172 教训)。"
              "回滚: YAML 开关 false + systemctl restart (playbook.* 热重载穿不透), 数据留档。"
              "2026-10-15 审计读数: anchor_recorded=True 行 **1120 ≥ min_n=300** ✓ (覆盖 6 个 "
              "distinct 日 10-07~10-14) — 但预注册判据第一腿是「影子期 ≥10 个交易日」∧①-④, "
              "现仅 6 日 ⇒ **n 轴达标 ≠ 判据达标** (双轴陷阱, 同 eth_relative_volume_gate 10-08 "
              "形态); 未擅改 min_n (改=重裁), ADJUDICATED 已挂 10-15 说明, ~10-20 满 10 交易日。",
    ),
    ShadowItem(
        id="execution_tca_spread_shadow",
        name="执行 TCA + 点差/报价年龄门只读影子 (RFC-152 Phase 0)",
        status=ShadowStatus.OBSERVING,
        description="每笔真正走到下单调用的指令落一行 data/shadow/execution_tca.jsonl: "
                    "计划锚/重定价锚/到达双边报价/点差(bps)/报价年龄(ms)/成交价/方向归一偏差。"
                    "0=只读测量: 不拦截、不改路由/仓位/SL/TP/退出; provisional_would_block "
                    "只是 jsonl 标签, 不是生效门。用于量化 implementation shortfall 与"
                    "点差门的反事实, 先回答'点差门能省多少、会误杀多少'。",
        data_path="data/shadow/execution_tca.jsonl",
        config_key="playbook.execution_parameters.execution_tca_shadow_enabled",
        min_n=30,
        count_mode="where:success=True",
        graduation_criteria="预注册见 RFC-152 §5: 样本门 ≥10 个 ET 交易日且 ≥30 笔成功成交; "
                            "必报点差/报价年龄分桶的扣费净 bps + 按日聚类 t + MDE80 + 误杀计数; "
                            "Phase 1 提案门 = 宽点差组净 bps 显著为负(日聚类 t≤-2.0)+宽点差组 "
                            "n≥30 且跨≥10 日+阈值前半窗定/后半窗验; **本 Phase 0 不设自动转生产**, "
                            "任何点差门生效须另立 RFC + 用户批准并占用 OKR-2026-10 C1 名额。",
        eval_script="scripts/execution_tca_report.py",
        eval_on_demand=True,
        created="2026-10-20",
        notes="2026-10-20 用户裁「同意，按照建议执行」落地 Phase 0。"
              "阶梯: 一杆秤(本项) → 点差/报价年龄门 → RS → ORB/ATR 衰竭 → 事件避雷针; "
              "本批只做第一步+点差标签, 其余未开工。"
              "为什么先做: 近 30 日毛利 +9.64bps vs 费用 16bps, 净 -6.36bps; 10-19 5/8 chase、"
              "4/8 计划锚偏离>1%; 墓地单(MFE<0.3%, 24 笔)承担几乎全部亏损 — 先量成交层缺口。"
              "边界: SIM 成交不可直接外推实盘; book proxy 行 spread_bps=None 单列; "
              "只覆盖已过全部闸门的尝试(选择偏差); 计划锚缺失不反推。"
              "回滚: execution_tca_shadow_enabled=false + 重启; 数据保留。",
    ),
    # ── 既有影子 (Phase 0 / 7月) — 代表性事项, 非穷举 ──
    ShadowItem(
        id="entry_band_widen",
        name="入场带 ATR 宽化",
        status=ShadowStatus.OBSERVING,
        description="RFC-065: 入场带按 ATR 只放宽不收窄 (chase 上限 8%)。record-only 常开攒样。",
        data_path="data/entry_band_widen_shadow.jsonl",
        config_key="playbook.execution_parameters.entry_band_widen_shadow_enabled",
        # 2026-10-07 [DEB-20261007 方案B, 用户裁「按建议执行」] 判据串台修复:
        #   原 graduation_criteria 抄的是 DEB-20260806 锚限价影子 (limit_entry) 的主门
        #   (sign-flip B=10000), min_n=60 亦同批带入 — RFC-065 原文是 n≥30 + 单侧安全门。
        #   本次只改回 RFC-065 逐字原文 (docs/decisions/RFC-065-entry-band-vol-widen.md:74),
        #   未下调任何门槛数字 (30 = 文档原值, 非放宽)。守卫: tests/test_shadow_registry_criteria_provenance.py
        min_n=30,
        graduation_criteria="预注册见 RFC-065: widen 专属事件 n≥30、单侧安全门 (widen 组不显著劣于 flat)、分组事先固定、分时间窗",
        verdict_history=[{"date": "2026-10-04", "verdict": "NOT_ESTABLISHED_KEEP_SAMPLING",
                         "evidence": "settle_entry_band_widen.py 首跑: n=34 (台账36, 缺bars 2), RFC-065 n≥30 门已达; "
                                     "安全门过 (p_worse=0.058>0.025), 正向证据不足 (mean=-19.09/CI95含0/符号p=0.30) "
                                     "⇒ 按预注册顺延攒样, 不翻开关。"}],
        # 2026-09-27 [RFC-115] 摘除错轴 eval_script (原挂 limit_entry_shadow_replay.py):
        # 该重放器读的是**别族影子的源** data/limit_entry_shadow.jsonl (189 行, 属 `limit_entry`
        # 影子, 8/29 用户裁 KILLED 停采 ⇒ 8/28 23:20 后零新增), 而本条 data_path 指的是活源
        # entry_band_widen_shadow.jsonl (30 行, 8/19→9/25 在写)。两文件字段几乎无交集
        # (交集仅 date/direction/inst_id/symbol/ts; 重放器依赖的 anchor/trigger_price/entry_path
        # 全是死源独有) ⇒ 重放器**读不到本条影子**, 其产物 variants (chase_5m +3.88 /
        # chase_30m −2.08) 是 `limit_entry` 那族的读数, 与本条 RFC-065 判据 (双事件 new/changed
        # + widen/flat path) 无关。挂错 eval_script 比不挂更坏: 待裁面会拿别族死源数字当本条
        # 证据。改为 NO_EVAL_SCRIPT 挂账 (照 ue_short_boost_shadow 惯例), 达门前建真结算器。
        # 2026-10-04 [RFC-065 缺口填补] 用户裁「按建议执行」: 建 scripts/settle_entry_band_widen.py
        # (读活源 entry_band_widen_shadow.jsonl, 事件级 widen-vs-flat 反事实回放, 判据字面出自
        # RFC-065: sign-flip B=10000 单侧α=0.025 + 双条件 + 符号共门 + 日聚类t + 尾部诊断)。
        # eval_on_demand=True: 与 short_funnel_shadow 同款 — 结算器是**待裁项出读数用**的按需
        # 评估器, 非周期环, 不挂 cron (哨兵豁免); 达门自动裁 = refresh_counts 迁移 READY 时
        # 由待裁面/人工跑本脚本呈裁。首跑 2026-10-04: n=34 (台账36, 缺bars 2), RFC-065 n≥30
        # 门已达, 判决 NOT_ESTABLISHED_KEEP_SAMPLING (安全门过 p_worse=0.058>0.025, 正向证据
        # 不足 mean=-19.09/CI95含0/符号p=0.30) ⇒ 按预注册顺延攒样, 不翻开关。
        # 2026-10-07 串台修复后 min_n=30 与结算器 MIN_N_RFC=30 同轴 (原 60 是 DEB-20260806
        # 的锚, 造成「按文档口径已过门、按注册表口径差 22 行」的双轴并存, 现已消除)。
        eval_script="scripts/settle_entry_band_widen.py",
        eval_on_demand=True,
        created="2026-08-06",
    ),
    ShadowItem(
        id="limit_entry",
        name="锚限价影子",
        status=ShadowStatus.KILLED,
        description="触发时记录锚限价入场 vs 实际市价入场的改善量。2026-08-29 用户裁决 KILL 停采。",
        data_path="data/limit_entry_shadow.jsonl",
        config_key="playbook.limit_entry_shadow_observe",
        min_n=60,
        graduation_criteria="DEB-20260806: sign-flip B=10000 单侧α=0.025 + 双条件(δ̄≥$1且CI95下界>0) + 符号共门",
        eval_script="scripts/limit_entry_graduation_gate.py",
        created="2026-08-06",
        last_evaluated="2026-08-23",
        verdict_history=[{"date": "2026-08-23", "verdict": "NOT_GRADUATED",
                         "evidence": "n=69配对真被动: δ̄=$3.03但σ=$28(功效预估σ=$4.29的6.5倍) → 置换p=0.19>0.025, CI95[-2.97,+10.31]含0, 符号检验37/69 p=0.32。三关全未过。按停止规则只降级不重置, 继续攒样, 每周五机械判定。"},
                        {"date": "2026-08-29", "verdict": "KILLED",
                         "evidence": "用户知情推翻预注册'只降级不重置'规则强杀 (提案方曾误述规则为应KILL, 更正后用户仍裁决杀)。"
                                     "依据: 8/23三关全败且σ超功效预估6.5倍=信号噪声比绝望; 停采 limit_entry_shadow_observe=false。回滚: 开关翻true+状态revert。"}],
    ),
    ShadowItem(
        id="vwap_gate_ab",
        name="VWAP 方向门 A/B 实验",
        status=ShadowStatus.OBSERVING,
        description="wrong_side 拦截 50/50 分流: bypass 臂放行成交, gated 臂维持拦截。",
        data_path="data/vwap_gate_ab.jsonl",
        config_key="playbook.execution_parameters.vwap_gate_ab_enabled",
        min_n=30,
        graduation_criteria="双臂净差 t-test + 按日聚类 MDE。"
                            "〔2026-10-22 RFC-158 修订指针: FADE_SHORT archetype 轴改由 RFC-158 "
                            "预注册接管 (每 archetype 每臂 n≥30, 切片评估 eval_vwap_gate.py "
                            "--by-archetype; 台账 entry_archetype 字段 10-21 起有值); "
                            "原双臂总体判据对非 FADE_SHORT 人群不变。改判据=重新裁决纪律 "
                            "已由 RFC-158 预注册满足 (用户裁「按建议执行」10-22)。〕",
        eval_script="scripts/eval_vwap_gate.py",
        created="2026-08-07",
        last_evaluated="2026-09-21",
        verdict_history=[{"date": "2026-09-21", "verdict": "不可分辨 (判据补齐后原'倾向拦错'不成立)",
                          "evidence": "清账批 (RFC-092) 补齐判据要求的按日聚类统计 (原 eval 只出均值+中位): "
                                      "处理组(被拦 wrong_side) 反事实 n=130 笔均 +0.1765% → **按日聚类 (41 天) "
                                      "day_mean +0.1403%, t=1.012, MDE(80%)=±0.3882 ⇒ 不可分辨** "
                                      "(MDE 是效应的 2.2 倍; 最大单日占比 6.2% 非单日驱动; drop-3-largest "
                                      "后 +0.1553% 稳健)。对照组(放行) 已实现 n=359 笔均 -0.0051%, "
                                      "t=0.304, MDE±0.2178 亦不可分辨。两臂日聚类差 +0.159% t=1.029 MDE±0.431 "
                                      "(且两臂测量基不同: 处理组分钟级反事实未持到收盘, 对照组全持仓期 ⇒ "
                                      "不可当因果推断)。⇒ 门'扼杀盈利'的说法在本标准下**不成立** (点估计为正但功效不足), "
                                      "维持 OBSERVING 继续攒样, 不改门。"}],
        eval_on_demand=True,  # eval=裁点触发 (样本到量才跑双臂t检验), 非周期挂载; 挂载哨兵豁免 (2026-09-01)
    ),
    ShadowItem(
        id="gap_chase_gate",
        name="追涨极端缺口门 (aligned_gap≥5%)",
        status=ShadowStatus.OBSERVING,
        description="2026-08-29 用户裁决注册 (MFE漏斗审计①队列): 顺向 aligned_gap≥5% 追缺口入场的"
                    "反事实拦截影子。回溯基线(7/1起): n=10, 笔均PnL -$28.3 (全场最差档); 导火索=GAP 8/28 "
                    "+18.95% gap 追入(thesis自注'严禁追涨'仍成交), MFE+5.9%→当日-3.6%过夜。"
                    "数据源=trade_records.metadata_json.entry_gap (入场时已采集, 零新采集线)。",
        data_path="data/shadow/gap_chase_gate_latest.json",
        config_key=None,
        min_n=30,
        graduation_criteria="预注册 (2026-08-29): n≥30 且 拦截组(aligned_gap≥5%)笔均PnL<0 且 "
                            "单侧符号检验 p≤0.05 → 毕业转硬门; 否则继续攒样不重置, 随周检机械判定。",
        eval_script="scripts/gap_chase_gate_eval.py",
        created="2026-08-29",
        last_evaluated="2026-09-21",
        verdict_history=[{"date": "2026-09-21", "verdict": "KEEP_SAMPLING",
                          "evidence": "清账批 (RFC-092) 实跑 eval: 覆盖率 80%, 拦截组 n=31 (门 30 ✓) "
                                      "笔均 **-$16.6** (门 <0 ✓) vs 对照组 n=384 笔均 -$0.3; "
                                      "单侧符号检验 p=0.9252 (门 ≤0.05 ✗) ⇒ 判据 2/3, 继续攒样。"
                                      "⚠️ 同时修正一个计数假象: 本项 data_path 原指向 .db, "
                                      "refresh_counts 对非 json/jsonl 路径返回 None ⇒ current_n 恒 0, "
                                      "被清账误读成'0 样本断供' — 实际喂数方正常 (415/864 笔含 "
                                      "entry_gap.aligned_gap_pct)。已改指 latest.json 计数锚。"},
                         {"date": "2026-08-29", "verdict": "BASELINE_KEEP_SAMPLING",
                         "evidence": "注册基线(7/1起): 拦截组 n=10 WR=50% 笔均-$28.3 累计-$283; "
                                     "对照组 gap<2% n=252 笔均-$3.6。n<30 且亏损笔数5/10未达显著 → 攒样中"}],
        notes="计数口径=拦截组交易笔数 (eval 脚本从 DB 现算); 与 gap_dive_halve (逆势深跌减半) 互补不对称: "
              "本项管顺向追极端缺口。"
              "🔗 2026-09-26 RFC-109⑥ 标同轴: 与 extreme_gap_ban_shadow 的**顺向腿 (ag≥+4)** 测同一"
              "假设的同一批笔 (顺向追极端缺口), 但阈值 (≥5% vs ≥+4%) 与判据 (本项要符号检验 p≤0.05, "
              "彼项要误杀率≤40%) 均不同 ⇒ 同批笔挂两个判据 = 双转正打架风险高于共用文件那对。"
              "裁决纪律: 三条**不得独立转正**, 顺向缺口禁入只允许一条胜出 (归属待用户裁)。",
        intent="gap_direction_alignment",
        cross_ref=("gap_alignment", "extreme_gap_ban_shadow"),
    ),
    ShadowItem(
        id="da_glm_shadow",
        name="DA glm-5.2 影子攒样 (SHORT 产出意愿)",
        # 2026-09-15 窗口收官: OBSERVING → READY_TO_EVALUATE (机械裁决已出, 待用户终裁)。
        # 采集环同日退役 (crontab 行已摘), 故 eval 改按需触发 (无周期挂载面)。
        # 2026-09-15 **用户终裁「停止 da_glm_shadow 不转正」** ⇒ READY_TO_EVALUATE → SUPERSEDED:
        #   归档留痕 (脚本与数据保留 = 停用非删证), 对应 DEB-20260915 方案 B。
        status=ShadowStatus.SUPERSEDED,
        eval_on_demand=True,
        description="2026-08-30 用户裁'1要' (05a69c43 落地), 2026-08-30 第9轮 9.1 补注册。"
                    "检验 8/30 多模型对比中 glm-5.2 的唯一显著分歧: 生产 4/4 里唯一给 SELL_SHORT 的是 glm。"
                    "架构纯旁路: 读生产 bridge 候选池 → 独立 collect_all (输入同构) → "
                    "glm-5.2 分析师 Pass1 (生产同款 SYSTEM_PROMPT, temp=0.3) → append jsonl。"
                    "零落库/零 bridge 写/零指令触碰。9.6: 材料漂移>1.0pp 标 tainted 剔除分歧归因。",
        data_path="data/shadow/da_glm_shadow.jsonl",
        config_key=None,
        min_n=10,
        count_mode="where:run_mode=live",
        graduation_criteria="预注册 (窗口 8/31-9/14, 10 交易日): "
                            "①JSON 成功率≥90% ②glm SELL_SHORT 提案≥5 笔 → PROMOTE_PROPOSAL 另立 DEB; "
                            "任一不过 → ARCHIVE 结案。③SHORT 提案 T+1 方向均值>0 仅当 n≥20 出结论 "
                            "(9.5 修正: 原 n≥5 ≈ 抛硬币), n<20 只作立项参考不作证据读。"
                            "窗口 9/14 第 10 采集夜收口后, 9/15 06:35 一次性机械裁决 "
                            "(cron DA-GLM-ONE-SHOT 跑完自删行, 产物 latest json + 飞书推送; "
                            "2026-09-01 复查收口补挂 — 原注册'9/14 裁决'措辞勘误: 裁决须在窗口"
                            "最后一夜采集之后, 且此前 eval 悬空无任何挂载), 周检可见。",
        eval_script="scripts/da_glm_shadow_eval.py",
        created="2026-08-30",
        last_evaluated="2026-09-15",
        verdict_history=[{"date": "2026-08-30", "verdict": "BASELINE_NO_DATA",
                         "evidence": "补注册基线: 现仅 1 行 run_mode=test (COIN 干跑复现分歧 "
                                     "prod=LONG vs glm=SELL_SHORT, trig=2, finish_reason=stop), "
                                     "无 live 样本; 8/31 起攒样"},
                        {"date": "2026-09-15", "verdict": "PROMOTE_PROPOSAL (机械, 待用户终裁)",
                         "evidence": "窗口 8/31-9/14 收官读数 (data/shadow/da_glm_shadow_eval_latest.json, "
                                     "09:35 CST 一次性机械裁决, 已推飞书): n_live=100, JSON 成功率 "
                                     "100% (①过); 去污后 SHORT 提案 20 笔 (②过, 门槛 5); ③n_t1=18<20 "
                                     "仅作参考 (方向均值 +0.2246%, 不作证据读)。预注册'①②全过 → "
                                     "PROMOTE_PROPOSAL 另立 DEB' → 立 DEB-20260915-da-glm-short-supply-promote "
                                     "呈裁。证据面 (窗口内 n=100): glm 方向 BUY 71/SELL_SHORT 21/SKIP 8 vs "
                                     "生产 LONG 87/SHORT 13; 分歧 agree 72 / glm_SHORT_prod_not_SHORT 14 / "
                                     "SKIP_vs_LONG 8 / LONG_vs_SHORT 6 — 立项假设'glm 更愿做空'方向成立 "
                                     "(21 vs 13 且 14 次独家看空), 但'生产 4/4 唯一给 SELL_SHORT'的量级表述 "
                                     "被证伪 (生产也给了 13 次); 有效性判据③ n=18<20 未成立 ⇒ 本裁决只证"
                                     "'会不会', 未证'值不值得' (不代裁)。"},
                        {"date": "2026-09-15", "verdict": "SUPERSEDED (用户终裁「停止/不转正」)",
                         "evidence": "用户裁「停止 da_glm_shadow 不转正」(2026-09-15) ⇒ 终态 "
                                     "SUPERSEDED 归档留痕 (非 GRADUATED/KILLED)。理由面: 机械裁决 ①JSON "
                                     "成功率 100% ②去污后 SHORT 提案 20 笔均过门, 但有效性判据③ n=18<20 "
                                     "(A6) 未成立; 且立项前提'生产唯一给 SHORT'的量级表述被证伪 (glm 21 vs "
                                     "生产 13) ⇒ **只证'会不会', 未证'值不值得'**。DEB-20260915 方案 B "
                                     "(归档) 生效; 方案 C (C1 SHORT 二读器 / C2 新窗口续验) 未采纳。数据 "
                                     "jsonl 与两个脚本**保留** (归档=停用, 非删证; 若日后重开须新预注册 + "
                                     "重新注册, 禁用窗口外样本补样)。"}],
        notes="计数口径=live 行数 (count_mode=where:run_mode=live — 2026-09-01 复查收口修正, "
              "原 lines 口径把 test 行也计入); cron '30 20 * * 1-5' (timeout 9000, "
              "9.2 修正自 3000) 是唯一采集线也是唯一 kill switch; "
              "判据③降级记录见 scripts/da_glm_shadow.py docstring (9.5)。"
              "2026-09-01 第11轮 taint 双修复留痕: 判污改双侧 effective 口径配对 "
              "(旧影子侧 last/prev 推导盘前恒≈0 与生产 effective 口径异基 → 8/31 "
              "误污 3/7 含 BABA SHORT 误杀) + 生产值采集时随行捕获 prod_snapshot_"
              "change_pct (检查点 _CKPT_KEEP_DAYS=3 过期免疫); 旧行缺一不判, "
              "n_taint_unevaluable 单列。见 DEB-20260829 第11轮。"
              "2026-09-15 收口 (用户裁「按照建议执行」): ①采集环退役 — crontab "
              "'30 20 * * 1-5' 两行 (注释+行) 已摘 (备份 /tmp/crontab.bak_20260915_glm_retire), "
              "脚本与数据保留 (退役=停采, 非删证); ②窗口污染堵口 — 9/15 20:30 采集环因 kill "
              "switch 未拉而多跑一夜 (窗口外 16 行, n 100→116), eval 已加窗口上界过滤 "
              "(WINDOW_END=2026-09-14, 越窗行单列 n_out_of_window 不入读数), 重跑不再污染; "
              "③棘轮基线 da_glm_shadow.py / da_glm_shadow_eval.py 两行已删 (环下线同步删, "
              "test_crontab_baseline_has_no_ghost_entries 由红转绿); ④终态: 2026-09-15 用户裁"
              "「停止 da_glm_shadow 不转正」⇒ **SUPERSEDED** (归档留痕: 脚本与数据保留, "
              "采集环已退役; 方案 B 生效, C 未采纳)。"
              "教训 (留痕): 预注册窗口与采集环是两个开关, 裁决器自删 ≠ 采集器退役; "
              "凡'窗口制'影子, 注册时须写明窗口末夜之后由谁摘采集行。",
    ),
    ShadowItem(
        id="da_prompt_distill_shadow",
        name="DA 系统提示词蒸馏影子 (DEB-20260901c 阶段二)",
        status=ShadowStatus.KILLED,
        description="2026-09-01 用户裁'批准两阶段方案'。阶段一 (行为等价审计) 已过: "
                    "37/37 行为标记全保留, 减重 19.0% (2867→2323 字符), 产物=仓外 "
                    "skills/deep-stock-analysis/scripts/prompt_distill_v2.py。"
                    "阶段二架构纯旁路: 读当晚 da_checkpoint raw.json 生料 → "
                    "DeepAnalyzer+V2垫片 (llm_synth.synthesize system_override, "
                    "组装链路与生产逐字节同源, 唯一变量=system 文本; 思考模式双臂同开) → "
                    "append jsonl; V1 对照=当晚生产 checkpoint 不重跑。"
                    "零落库/零 bridge/零指令。",
        data_path="data/shadow/da_prompt_distill_shadow.jsonl",
        config_key=None,
        min_n=10,
        count_mode="where:run_mode=live",
        graduation_criteria="预注册 (窗口 9/1-9/15, 10 交易日, 不含 9/7 劳动节): "
                            "①JSON 有效+契约违约率≤V1 ②direction 比例与 HIGH 档比例漂移≤10pp "
                            "③T+1 配对 alpha 非劣 (差值下界≥-0.10%) ④token 降幅≥15%。"
                            "⓪行为等价 checklist 已于阶段一通过 (一票否决线, 前置)。"
                            "全过 → 另立执行 DEB 换生产 SYSTEM_PROMPT+换 PROMPT_HASH 锚点; "
                            "任一不过 → ARCHIVE (仅④不过且③正向 → 按③改善另议, 不自动结案)。"
                            "9/16 一次性评估 (评估脚本已落地: "
                            "scripts/da_prompt_distill_shadow_eval.py, cron 35 6 16 9 * "
                            "自删行, 2026-09-01 红队复查批)。",
        eval_script="scripts/da_prompt_distill_shadow_eval.py",
        created="2026-09-01",
        last_evaluated="2026-09-21",
        verdict_history=[{"date": "2026-09-01", "verdict": "BASELINE_NO_DATA",
                          "evidence": "注册基线: 影子脚本落地, 首个采集夜 9/1 (raw.json "
                                      "首产夜), 评估脚本与一次性裁决 cron 随窗口收口批落地"},
                         {"date": "2026-09-01", "verdict": "MAINTENANCE",
                          "evidence": "红队复查批: ①窗口措辞勘误 9/2-9/15→9/1-9/15 "
                                      "(首采夜=9/1 ET — 21:40 CST 影子读当晚 checkpoint "
                                      "目录; 原口径实为 9 交易日与'10 交易日'自相矛盾); "
                                      "②eval 脚本 da_prompt_distill_shadow_eval.py + "
                                      "一次性 cron 35 6 16 9 * "
                                      "(DA-DISTILL-ONE-SHOT-ADJUDICATION) 落地, 悬空清零; "
                                      "③采集同批起捕获 V1 归因三元 (finish_reason/契约违约/"
                                      "entry_price) + 双臂 tokens_out — 生产检查点 "
                                      "_CKPT_KEEP_DAYS=3 自删, 判据①③评估不再依赖窗口末重读"},
                         {"date": "2026-09-21", "verdict": "KILLED (ARCHIVE)",
                          "evidence": "清账批 (RFC-092): 逐字重跑预注册判据 "
                                      "evaluate(load_rows()) — n_live=198, ①契约违约率 v2=6.06%% "
                                      "vs V1=1.52%% ✗, ④token 降幅 2.57%% < 15%% ✗, ②漂移 5.1pp/2.0pp ✓, "
                                      "③T+1 配对 n=123 差值 +0.016%% ✓ ⇒ 任一不过 → ARCHIVE (判据明文)。"
                                      "采集 cron 已于 2026-09-17 用户裁'停镜像'注释摘除; "
                                      "本次只是把注册表状态同步到已执行的决定。"}],
        notes="计数口径=live 行数; cron '40 21 * * 1-5' 是唯一采集线也是唯一 kill switch "
              "(思考模式下 DA 全轮延后, 21:40 发车留足完成余量)。eval 脚本与一次性裁决 "
              "cron 已于 2026-09-01 红队复查批落地 (悬空清零)。DEB-20260901b (思考模式) "
              "已直切生产, 与本影子不构成变量纠缠 (双臂同思考态, 单变量=system 文本)。",
    ),
    ShadowItem(
        id="e2_momentum",
        name="E2 入场动量确认",
        status=ShadowStatus.KILLED,
        description="入场后 15min MFE 确认 (θ=0.5%) 反事实。record-only 不拦。**2026-09-13 用户裁退役**: 采集cron已摘, 数据留档242行, 毕业门恒判 RETIRED_NO_PROMOTION。",
        data_path="data/e2_momentum_shadow.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="DEB-20260807 §5.1: 拦截组净亏+放行组盈利模式成立 → 提请用户裁决转硬门; 统计按宪章第9律日聚类+MDE",
        eval_script="scripts/e2_momentum_graduation_gate.py",
        created="2026-07-20",
        last_evaluated="2026-09-12",
        verdict_history=[{"date": "2026-08-23", "verdict": "PATTERN_DIRECTIONAL",
                         "evidence": "前向n=129: 拦截n=80净-$1589.6(均-$19.87,WR33%) vs 放行n=49净+$1221.7(均+$24.93,WR65%); 日聚类15配对日 diff=-$34.8 t=-2.67<MDE$51.6 → 方向强但不可分辨。字面判据(拦截组全亏)不成立(WR33%)。转硬门与9月退出参数冻结冲突, 建议10月初裁。"},
                        {"date": "2026-08-23", "verdict": "USER_DEFERRED",
                         "evidence": "用户裁决'同意': 不转硬门, 压至10月初(冻结窗口结束后)再裁; 影子继续攒样不重置, 周检机械跟踪。"},
                        {"date": "2026-09-12", "verdict": "USER_CONTINUE_SAMPLING",
                         "evidence": "毕业门 9/11 复跑(只读): PATTERN_CONFIRMED_DIRECTIONAL — 放行组净+$2290.8(WR78.4%), 拦截组净亏; 日聚类 n=26 diff_mean=-$29.37 t=-2.742 < MDE=42.42 → significant_beyond_mde=false。用户裁'按建议执行'=继续攒样不转硬门 (统计不可分辨, 不满足双窗口≥40%采信门槛); 维持压至10月初冻结窗结束再裁, 采集不重置。"},
                        {"date": "2026-09-13", "verdict": "KILLED",
                         "evidence": "用户裁「无效就停止，退役」。裁决前全量复核(/tmp/e2_deep.py): "
                                     "①统计不可分辨 日聚类t=-2.897但|diff|=29.91<MDE=40.88; "
                                     "②OOS衰减归零 IS(7/28-8/19)拦截组均-$25.15 → OOS(8/20-9/11)均+$0.21 — 现役门(DA v2.4禁接飞刀/RFC-082)已提前拦掉最烂入场, E2人群被覆盖残差趋零; "
                                     "③误杀率50.7% 拦截组138笔中70笔盈利(+$1791, 含SL_TRAIL_LOCK赢家21笔)=砍在利润引擎起涨前; "
                                     "④SHORT仅6拦/9放, 宪章方向对称无法验; "
                                     "⑤实现语义差距 影子=全程退出净额 vs 硬门=15min提前出场价, 不可平移。 "
                                     "与 R-Breaker 独立回放同向: 现役过滤栈已把烂入场拦得差不多, 第二道入场门边际价值快速下降。"},
                        ],
        notes="2026-09-13 用户裁 KILLED: 采集cron(e2_momentum_shadow_daily 20 5 * * 2-6)已摘, "
              "数据 data/e2_momentum_shadow.jsonl 242行留档不删; 毕业门恒判 RETIRED_NO_PROMOTION; "
              "回滚须另开辩论改判, 不得仅删开关 (同 premarket_invalidation A/C 退役先例)。",
    ),
    ShadowItem(
        id="overnight_hold",
        name="隔夜持仓影子",
        status=ShadowStatus.GRADUATED,
        description="benign 退出 → 持仓过夜反事实。已毕业转 loser_overnight_enabled。",
        data_path="data/overnight_hold_shadow.jsonl",
        config_key="playbook.overnight_hold_shadow_enabled",
        min_n=15,
        graduation_criteria="回放净正 (V1/V2/V3 全正)",
        eval_script="scripts/overnight_arm_report.py",
        created="2026-08-15",
        last_evaluated="2026-08-29",
        verdict_history=[
            {"date": "2026-08-15", "verdict": "GRADUATED",
             "evidence": "W_WEEK +$629 (n=15, 修复率73%)"},
            {"date": "2026-08-29", "verdict": "PROD_KILL_CRITERION_REGISTERED",
             "evidence": "用户裁'5批': 生产判罪线预注册 — w_week 臂已平 n≥20 且修复率<40% "
                         "→ 裁排除 loser_overnight_enabled。跟踪: overnight_arm_report "
                         "预注册判据段 (当前 n=4, 修复率75%, 净PnL -$207, 攒样中)"}],
        notes="毕业后生产表现由 RFC-067 台账 + overnight_arm_report 预注册判据跟踪。"
              " [2026-09-16 代码退役] 写入方自 2026-08-28 置 false 停采；结算器自 "
              "2026-09-10 配置停并观察一周后，已于 2026-09-16 删除调度挂载、脚本与死开关。"
              "历史 jsonl 保留；live 判罪线继续由 overnight_arm_report 读取 DB metadata。",
    ),
    ShadowItem(
        id="overnight_kill_criterion",
        name="隔夜臂判罪线 (w_week 生产跟踪)",
        status=ShadowStatus.SUPERSEDED,
        description="overnight_hold 毕业后的生产判罪线 (2026-08-29 用户裁'5批' 预注册): "
                    "w_week 臂已平仓 n≥20 且修复率(结局 pnl>0)<40% → 裁排除 "
                    "loser_overnight_enabled。2026-09-01 复查收口立项: 此前判据只在 "
                    "overnight_arm_report 的 stdout 里, 无 cron 挂载无文件落盘, 对周检/"
                    "哨兵等一切自动化不可见 (GRADUATED 项被哨兵跳过) — 现单独注册为 "
                    "OBSERVING 项接管跟踪 (判据字典 B15)。",
        data_path="data/shadow/overnight_arm_report_latest.json",
        config_key=None,
        min_n=20,
        graduation_criteria="B15: n≥20 且修复率<40% → 提请裁排除 loser_overnight_enabled "
                            "(回滚=开关翻回 true); n≥20 且修复率≥40% → 维持, 翻 "
                            "SUPERSEDED 留痕; n<20 攒样, 周检机械跟踪不重置。",
        eval_script="scripts/overnight_arm_report.py",
        created="2026-09-01",
        last_evaluated="2026-09-21",
        verdict_history=[{"date": "2026-09-21", "verdict": "SUPERSEDED (维持 loser_overnight_enabled)",
                          "evidence": "判据 B15 实测: w_week 臂已平 n=23 (门 20, ✓) 修复率 69.6%% "
                                      "(门 <40%%, ✗ 未触发排除线) → 判据明文'维持, 翻 SUPERSEDED 留痕'; "
                                      "汇总落盘 data/shadow/overnight_arm_report_latest.json "
                                      "verdict=REPAIR_RATE_OK (2026-09-21 07:34 重跑复核一致)。"
                                      "SUPERSEDED 在此为『结案』语义 (无替代项), 非被新版取代。"}],
        notes="✅**已裁维持不停 (2026-09-26 用户裁「5 不停」, 依据 RFC-109⑤ 盘点: 停采须改生产码/拆写方, 而负载微小 ⇒ 风险>收益)。** 原盘点 (L3 提案, 未执行): data_path 指向的 "
              "overnight_arm_report_latest.json **不是采集产物** — 是 scripts/overnight_arm_report.py "
              "的只读报表输出, 且**同时是本条注册表计数锚** (顶层 n=已平仓数, n≥20 翻 READY) 与 "
              "B15 判罪线可见面 ⇒「停采」=撤 cron 20 6 * * 2-6, 会连带断计数锚与判罪线 "
              "(不是减负载, 是断证据)。负载 4K/15行, 近7日+0 ⇒ 停采收益≈0, 建议不停。"
              "2026-08-29 注册表 verdict 'PROD_KILL_CRITERION_REGISTERED' 的执行落地; "
              "cron 20 6 * * 2-6 判罪线进度 JSON 落盘 (顶层 n=已平仓数, 注册表计数锚); "
              "原 overnight_hold 项保持 GRADUATED (毕业案历史), 本项只管毕业后生产表现。",
    ),
    ShadowItem(
        id="mfe_stall_exit",
        name="MFE Stall 退出",
        status=ShadowStatus.KILLED,
        description="持仓150min+MFE<0.5%即出场。DEB-20260815 否决。",
        data_path="data/high_atr_stall_shadow.jsonl",
        config_key="playbook.execution_parameters.mfe_stall_exit_enabled",
        min_n=30,
        graduation_criteria="p<0.05 + 捕获率>30%",
        eval_script=None,
        created="2026-08-08",
        last_evaluated="2026-08-15",
        verdict_history=[{"date": "2026-08-15", "verdict": "KILLED",
                         "evidence": "p=0.17 捕获率14% n=31 否决; 7/1起实跑-$1353"}],
        notes="✅**已裁维持不停 (2026-09-26 用户裁「5 不停」, 依据 RFC-109⑤ 盘点: 停采须改生产码/拆写方, 而负载微小 ⇒ 风险>收益)。** 原盘点 (L3 提案, 未执行): 写方 exit_engine:860 **无门控** "
              "(在 ATR≥4%∧peak<0.3%∧profit<0 分支内无条件写) ⇒ 停采须加 shadow-only 键 + 重启; "
              "⚠️ config_key=mfe_stall_exit_enabled 是**生产退出腿**(现false), 不门控影子写方 — 禁碰。"
              "负载 92K/300行, 近7日+34 (5 条里最轻)。",
    ),

    # ── 退出策略核查批 (2026-08-31, 用户裁"影子先行") ──
    ShadowItem(
        id="reentry_quick_tp_gate",
        name="再入场快TP门禁吞掉",
        status=ShadowStatus.KILLED,
        description="8/7 裁决的再入场快收 TP (RR≈0.58) 被 7/10 min_tp_rr=1.5 门禁静默吞掉, "
                    "13 笔实弹 0 笔 TAKE_PROFIT。影子记'价格触及被吞TP'反事实 + 平仓结算差, "
                    "回答'恢复快TP还是废quick_tp参数'。两次裁决互杀, 2026-08-31 退出核查发现。",
        data_path="data/shadow/reentry_quick_tp_shadow.jsonl",
        config_key="playbook.execution_parameters.reentry_quick_tp_shadow_enabled",
        min_n=10,
        graduation_criteria="窗口 9/1-9/30: n≥10 笔 TP 触达结算行 → tp_vs_actual_pct 均值>"
                            "费用线(~0.15%) ∧ ≥70%方向为正 → 提案恢复快TP(门禁按 decision_id "
                            "前缀豁免, 另立DEB+用户裁); 否则维持门禁+提案废 quick_tp 参数。"
                            "窗内禁改门禁/快收参数。",
        eval_script=None,
        created="2026-08-31",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[
            {"date": "2026-10-03", "verdict": "KILLED (窗满机械结算: TP 触达 0/13)",
             "evidence": "预注册窗 9/1-9/30 已闭 (B13: 到期一次性结算)。结算产物 13 行 reentry "
                         "实弹全部落盘, 其中 **tp_touched=0/13** ⇒ 判据轴「TP 触达结算行 n≥10」"
                         "实际 n=0, 落预注册「否则」分支: ①门禁维持 (现状即门禁, 零动作); "
                         "②quick_tp 参数在现行 min_tp_rr=1.5 门禁下**物理零触发** = 死参数, "
                         "其删除属 L3 生产变更, 本批仅提案不执行 (REVIEW-20261003 待裁面)。"
                         "停采: reentry_quick_tp_shadow_enabled false (真 shadow-only 键, "
                         "reentry_tp_shadow.py:42 热重载可读); 数据 13 行留档。回滚: 开关 true "
                         "+ 本条 revert OBSERVING。"},
        ],
        notes="count_mode=lines; 结算行含 is_reentry/touch/tp_vs_actual_pct; "
              "DEB-20260831-reentry-quick-tp-gate-conflict §四; 判据字典 B13。"
              "2026-10-03 清理批: 窗满结算判据轴 n=0 (0/13 触达) ⇒ KILLED; quick_tp 参数"
              "处置 (删除/保留) 待用户裁, 不在本批执行。",
    ),

    # ── 隔夜退出时机重构批 (2026-09-02, 用户裁"方案C同意") ──
    ShadowItem(
        id="winner_overnight",
        name="赢家隔夜影子 (PRE_CLOSE 盈利仓持一夜反事实)",
        status=ShadowStatus.OBSERVING,
        description="RFC-074: cohort A 盈利仓 15:52 一律强平, '赢家隔夜'臂事实死亡 "
                    "(long horizon 8月以来 0.5% 分配率)。arm 点挂钩 PRE_CLOSE_EXIT "
                    "(pipeline.py), 次日 1min bar 结算'带 SL 持一夜'结局 "
                    "(SL_gap/SL_touch/EOD_next_day)。历史反事实上界: 锁利型隔夜仓 "
                    "T+2 +$421/T+3 +$824 (n=3/2, 日bar收盘价口径)。",
        data_path="data/shadow/winner_overnight_shadow.jsonl",
        config_key="playbook.execution_parameters.winner_overnight_shadow_enabled",
        min_n=20,
        graduation_criteria="n≥20 盈利臂结算行 (arm_profit_pct>0, 毛口径 — 2026-09-02 "
                            "核查补: arm 点会捕非豁免亏损臂, 不入判据仅对照) → "
                            "overnight_delta 均值>费用线(~0.15%) ∧ "
                            "≥60%方向为正 ∧ SL_touch 占比<40% (防尾部) → 提案赢家隔夜臂 "
                            "(cohort A 盈利仓持夜豁免/或复活 long horizon, 另立DEB+用户裁); "
                            "否则维持当日落袋。窗内禁改 PRE_CLOSE/EOD 链。",
        eval_script="scripts/settle_winner_overnight_shadow.py",
        created="2026-09-02",
        notes="count_mode=settled_winner_arm_lines (arm_profit_pct>0, 毛口径; 汇总由"
              "结算脚本过滤, 亏损臂仅 loser_n 对照); 悲观序结算(不假设更优出场); "
              "2026-10-09 RFC-140 口径修复: overnight_delta 主口径从「初始SL冻结」改为"
              "「era-faithful 生效SL逐bar重放」(arm 行落 era_be/dd/cushion/adaptive 字段, "
              "结算器复用生产 SSOT _compute_dynamic_effective_sl 重放, BE武装/锁盈ratchet/"
              "冷却豁免同生产语义); 旧口径留档 overnight_delta_init_sl 双列; legacy arm 行"
              "无 era 字段 → fail-safe 回退初始SL并标 sl_caliber=init_sl_legacy。存量4笔"
              "重结算: 旧口径 +183.14/0次SL → 生效SL口径 -142.24/4次SL类出场(3 touch+1 gap), "
              "方向翻转实锤「带SL持一夜」判据此前被结构性高估。判据文/阈值逐字未动; "
              "DEB-20260902-overnight-exit-timing; 判据字典 B16",
    ),

    # ── DEB-20260901 Phase C 三观察点 (2026-09-01 用户裁"要"立项; 防散落: 全部注册于此,
    #    判据落字典 B14, 脚本落回放台账 — 不允许散落游离观察) ──
    ShadowItem(
        id="pullback_limit_shadow",
        name="确认位限价入场影子 (PULLBACK_LIMIT)",
        status=ShadowStatus.KILLED,
        description="DEB-20260901 Phase C 观察点①: 带确认位的 PULLBACK_LIMIT/FADE_SHORT 指令, "
                    "执行层按现状执行 (零行为变更) 同时落触发上下文影子 "
                    "(type=trigger/chase_cap_violation, _shadow_dedup_ok 文件级防重), "
                    "次日 cf_pullback_limit_replay.py 重放 would-fill/CF-PnL。"
                    "路由弹药已备好默认关 (eefba165, entry_archetype_routing_enabled=false)。",
        data_path="data/shadow/pullback_limit_shadow.jsonl",
        config_key="playbook.pullback_limit_shadow_enabled",
        min_n=20,
        graduation_criteria="DEB-20260901 §五 P0 预注册 (Phase C go/no-go): n≥20 可配对样本且三条全过: "
                            "①净期望改善≥30% ②成交率≥50% ③mfe=0 占比下降 → 提案执行层路由接线 "
                            "(翻 entry_archetype_routing_enabled=true, 需用户裁决); 任一条显著劣化 → 撤回, 证据留档。",
        eval_script="scripts/cf_pullback_limit_replay.py",
        created="2026-09-01",
        last_evaluated="2026-09-21",
        verdict_history=[{"date": "2026-09-21", "verdict": "KILLED (撤回接线)",
                          "evidence": "清账批 (RFC-092) 实测窗 9/8-9/18 (产物 "
                                      "data/shadow/pullback_limit_replay_v2_latest.json, 9/19 生成): "
                                      "n_paired=49 ≥20 ✓ | ①净期望改善 84.2%% ≥30%% ✓ | "
                                      "②成交率 27.1%% <50%% ✗ | ③mfe=0 占比 0.0→0.0 未下降 ✗ "
                                      "⇒ adr035_verdict 2/4, wire_routing=false; 另 D 误杀率 54.9%%>40%% ✗, "
                                      "日聚类 t=0.62 MDE ±20.5 ⇒ 与 0 不可分辨。判据明文'任一条显著劣化 → "
                                      "撤回, 证据留档' ⇒ 结案。若未来重启该假设, 必须新预注册 "
                                      "(读数卡已给可达性门方向的数据依据), 不得挪用本判据。"}],
        notes="✅**已裁维持不停 (2026-09-26 用户裁「5 不停」, 依据 RFC-109⑤ 盘点: 停采须改生产码/拆写方, 而负载微小 ⇒ 风险>收益)。** 原盘点 (L3 提案, 未执行 — **本条最易误伤**): 产物有"
              "**两个语义共用的写方** — ①pipeline:6276 受 pullback_limit_shadow_enabled 门控 "
              "(Phase B trigger 行); ②playbook_service._append_routed_shadow (DEB-20260901 选项1② "
              "archetype 路由遥测, **活跃且不受该开关门控**) ⇒ 关开关只停一半, 且路由遥测行断供 "
              "= 次日重放脚本按 type 取行的路由归因失效。真要停须先拆 type/拆文件。"
              "负载 436K/1257行, 近7日+258。"
              "首个生产夜=9/1 盘 (结构化字段首夜); 8/31 指令无结构化字段重放 n=0 合预期; "
              "首批判读 9/2 05:47; count_mode=lines。"
              "双口径窗口 (2026-09-02 DEB-20260902 议题二补): 判据 n≥20 可配对样本自 "
              "9/1 起算 (指令侧 DB 重构可信); chase_cap 违例率维度自 9/2 起算 (9/1 "
              "trigger 影子全灭 json.loads(dict) bug, 修复 7e4d51cb 于盘中, 当日该维度 "
              "输出 null 非零 — replay 报告已带口径注记)。",
    ),
    ShadowItem(
        id="gap_alignment",
        name="缺口对齐影子 (分源切片)",
        status=ShadowStatus.OBSERVING,
        description="DEB-20260901 Phase C 观察点②: 每笔 playbook 入场落 "
                    "aligned_gap_pct×archetype×窗口×fwd_return 影子; Phase C 扩展 "
                    "source=DA/UE/REENTRY/OTHER 分源切片 + mfe_pct 字段 (UE 左尾/mfe=0 监控)。"
                    "历史 390 笔已回填 (6/27-8/31, 与 DEB §0.3 逐格一致)。纯影子不拦截; "
                    "执行侧归 gap_chase_gate (B1), 本项不另立门。",
        data_path="data/shadow/gap_alignment_shadow.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="DEB-20260901 §五 P1: 各格 n≥30 后用 gate_cost_report.py 方法算拦截成本 "
                            "(被拦单 fwd_return_intraday 均值), 仅当拦截成本<保留收益才进入门禁立项; "
                            "执行侧毕业判据见 gap_chase_gate (B1)。",
        eval_script="scripts/gap_alignment_shadow.py",
        created="2026-09-01",
        notes="count_mode=distinct:trade_id — 重跑/回填不重复计数, 每笔交易只算 1 样本 "
              "(390 回填系合法历史样本=§0.3 证据基线, 非刷样); by_source 切片读 latest summary。"
              "🔗 2026-09-26 RFC-109⑥ 标同轴: 与 extreme_gap_ban_shadow **共用同一 data_path** "
              "(gap_alignment_shadow.jsonl, n 完全相同) ⇒ 同一批笔挂两个判据。裁决纪律: "
              "两条**不得独立转正** (双转正会打架), 归属先裁 — 执行侧门归 gap_chase_gate (B1), "
              "禁入提案归 extreme_gap_ban_shadow (D1'), 本条只做分源切片观测不另立门。",
        count_mode="distinct:trade_id",
        intent="gap_direction_alignment",
        cross_ref=("extreme_gap_ban_shadow", "gap_chase_gate"),
    ),
    ShadowItem(
        id="da_blocked_cf",
        name="DA 门被拦方向性信号前瞻评估",
        status=ShadowStatus.OBSERVING,
        description="DEB-20260901 Phase C 观察点③: DA 质量门拦掉的方向性候选 "
                    "(direction∈BUY/SELL_SHORT, SKIP 行排除) 若放行会怎样 — '拦太狠'自警 "
                    "+ BABA/BWXT $3000 地板误杀案的系统性反事实证据 (非个案叙事)。"
                    "数据源=da_gate_decisions.jsonl (7/13起) + replay_bars_daily 日K; "
                    "锚=锚日开盘价, 方向带符号收益, 右缘删失显式计数。只读幂等。",
        data_path="data/shadow/da_blocked_cf_latest.json",
        config_key=None,
        min_n=50,
        graduation_criteria="判据字典 B14 (2026-09-01 预注册): 主窗 T+1, n_eval≥50: "
                            "mean_signed≤-0.15% ∧ p_under≤0.05 → GATE_EFFECTIVE (拦得对, 维持); "
                            "mean_signed≥+0.15% ∧ p_over≤0.05 → OVER_BLOCKING_SUSPECT (误杀, 提案另立DEB); "
                            "否则 KEEP_SAMPLING。T+3/T+5 仅一致性披露, 不参与裁决 (防多窗拣选)。",
        eval_script="scripts/da_blocked_signal_cf_eval.py",
        created="2026-09-01",
        last_evaluated="2026-09-01",
        verdict_history=[{"date": "2026-09-01", "verdict": "KEEP_SAMPLING",
                         "evidence": "基线首跑: n=139(去重) 可评估126, T+1 mean=-0.088% WR=46.0% "
                                     "费用线内且不显著; T+3/T+5 方向为负且 p_under≤0.05 (一致性披露) "
                                     "→ 门方向上'拦得对', 无过度拦截证据"}],
        notes="cron 15 6 * * 2-6; 删失 8 笔=无日K缓存 (FBK/BTI/TRV/SNY/AMC/OFG/RDTL); "
              "计数锚=latest json 顶层 n (主窗 n_eval)",
    ),
    ShadowItem(
        id="ssr_shadow_daily",
        name="SSR 影子日更 (RFC-062 ④)",
        status=ShadowStatus.OBSERVING,
        description="RFC-062 在建清单④ (8/2 落地, 2026-09-01 全链体检发现 cron 悬空补挂): "
                    "盘后统计 SHORT 指令标的当日盘中是否触及 SSR 线 (跌≥10% 相对昨收), "
                    "给实盘化时门2 G2-2 (合规拒单) 提供先验分布。保守上界口径 "
                    "(day_low≤prev_close×0.90, 触及≠指令时刻必处SSR)。标记写 "
                    "raw_instruction_json.ssr_shadow, 幂等。8/1-8/24 悬空断档, "
                    "8/25 起已回填 (16 条标记, 3 触及, 含 8/31 PCG 崩跌)。",
        data_path="data/ftseek_us.db (playbook_instructions.raw_instruction_json.ssr_shadow)",
        config_key=None,
        min_n=30,
        graduation_criteria="无独立毕业门 — 先验分布采集: SHORT 指令日标记累计≥30 后, "
                            "实盘化前供 RFC-062 G2-2 合规拒单门设计引用 (触及率分布)。",
        eval_script="scripts/ssr_shadow_daily.py",
        created="2026-08-02",
        last_evaluated="2026-09-01",
        verdict_history=[{"date": "2026-09-01", "verdict": "BACKFILL_DONE",
                         "evidence": "体检发现悬空 (标记停在 7/31): 补挂 cron 25 6 * * 2-6 + "
                                     "回填 8/25-8/31 共 16 条标记 (3 触及: 8/25×1, 8/31×2 含 PCG)"}],
        notes="计数口径同 gap_chase_gate (DB 内标记, 注册表不计数, eval 现算); "
              "脚本自带 MarketDataRouter 拉日K, 盘后任意时点可跑。"
              "📐**真 n 读数 2026-09-26 (RFC-109⑤-B, 只读 SQL 复算)**: 注册面显示 n=0 "
              "**不是断供也不是尺子坏** — `_count_samples` 对 `.db` 型 data_path 返回 None "
              "(shadow_registry.py:2453, 刻意不伪装成 0/断供), 0 是 dataclass 默认值。"
              "按注册项声明的轴 (playbook_instructions.raw_instruction_json 含 ssr_shadow 键) "
              "只读复算 = **95 行标记 / 27 个日期** (2026-07-27 ~ 09-25), 判据「SHORT 指令日标记"
              "累计≥30」**早已达标 (95≫30)**。同期 direction 分布: SKIP 1849 / BUY 796 / "
              "SELL_SHORT 230。⚠️ eval 的 main **写库** (scripts/ssr_shadow_daily.py:103 "
              "`UPDATE playbook_instructions SET raw_instruction_json=?`) ⇒ 按副作用纪律**不整跑**, "
              "真 n 走只读 SQL。"
              "🔑**为什么不修计数器 (不建 DB 计数轴)**: ①判据自述「**无独立毕业门** — 先验分布采集」, "
              "达标 ≠ 要转正/接线, 它是给未来 RFC-062 G2-2 (合规拒单门) 实盘化时提供触及率分布的"
              "**原料面**, 不是一道要裁决的门 ⇒ 给它一个 READY 状态反而会造出一条永不结案的假待裁项; "
              "②给 `_count_samples` 加 DB 轴 = 新机制 (违反做减法), 且该函数正被并行 agent 改 "
              "(json_key 点号嵌套), 同期改同函数必冲突; ③现设计 (eval 现算 + notes 记真读数) "
              "已能让读数可见。⇒ 处置 = 留痕真读数, 不动计数器/不加轴/不改 status。",
    ),

    # ── DEB-20260903 议题二后续 (2026-09-03 用户裁"A") — 三登记之①注册表本体;
    #    判据落字典 B17, 两条环路落控制环路台账 §2.3/§2.4 (2026-09-03 复盘批补齐,
    #    缘起: 落地时三处登记全缺 = DEB-20260901 §十一 严令禁止的游离观察) ──
    ShadowItem(
        id="penetration_shadow",
        name="保本线穿透判别器 (快跌 vs V回)",
        status=ShadowStatus.OBSERVING,
        description="DEB-20260903 §十: 净保本类出场 (SL_BREAKEVEN/SL_TRAIL_LOCK/"
                    "PATIENT_EXIT_FILL) 触发瞬间采集判别特征 (穿透深度bps/自峰回吐/"
                    "是否刷新低/持仓时长/开盘以来分钟/ATR归一/侧/路径/quote_src), "
                    "次晨用离场后 1 分 bar 回填 recovered_1/3/5/10m + continued_bps_10m "
                    "+ cf_floor_fill_saved_bps; 周频 LLM 出题 + 确定性 replay 全样本判卷。"
                    "record-only 零交易行为变更 — 议题二 marketable limit 已独立生效, "
                    "本影子是它的审计尺, 不是它的开关。",
        data_path="data/shadow/penetration_shadow.jsonl",
        config_key="llm_features.penetration_shadow.enabled",
        min_n=20,
        graduation_criteria="判据字典 B17: n_labeled≥20 ∧ 某假设 matched≥8 ∧ precision≥0.7 "
                            "∧ 命中集内少数类(未V回)≥8 [2026-09-23 收紧, 类别失衡时 precision "
                            "门在基线上即过] → 自动开 DEB 草稿 + 推送呈裁 (仅立案, 写执行代码须用户裁); "
                            "议题二机制判据 (死等秒数=0 / 撤单转市价=0 / 成交滑点≤pad+容差) "
                            "逐事件即判, 不等 n; 回滚线见 DEB-20260903 §十二。",
        eval_script="scripts/penetration_llm_analysis.py",
        created="2026-09-03",
        last_evaluated="2026-09-13",
        verdict_history=[{"date": "2026-09-13", "verdict": "KEEP_SAMPLING",
                         "evidence": "判据实测(B17: n_labeled≥20 ∧ 某假设matched≥8 ∧ precision≥0.7): "
                                     "n_labeled=42 ✓ 但 hypotheses_scored=[] — LLM 出题环零可判分假设, "
                                     "故 matched/precision 无实测值。⚠️ latest.json 里的 "
                                     "graduation_gate={n:20,matched:8,precision:0.7} 是 GRAD_* 常量回显"
                                     "非实测结果, 不得当达标读 (2026-09-13 实锤)。基线: overall "
                                     "v_rate_5m=0.976 (active 1.000/passive 0.750) — 几乎全 V 回, "
                                     "判别器要分出'快跌不回'的少数族, 当前无假设能分。维持攒样。"},
                        ],
        notes="count_mode=where:type=penetration — 计采集事件数 (每事件 1 行 penetration "
              "+ ≤1 行 outcome, 用 lines 会双倍计数); 判分口径是已回填配对 n_labeled "
              "(analysis 脚本), 比本计数滞后 ≤1 夜。基率诚实披露: 家族 5 案/12 交易日 "
              "(8/18×1, 8/20×3, 9/2×1) 且 8/21→9/1 零案 → n≥8 首调 LLM 约需 19-80 交易日, "
              "n≥20 约 48+ 交易日; 本项是慢环, 不得当作议题二的判分前置。载体心跳: "
              "回填 cron 每日 touch jsonl → mtime 新鲜度=回填环活着 (断流>14天告), "
              "行数=样本量, 样本新鲜度看行内 et_date (看板段11 已按 et_date 分窗)。"
              "cohort 注记: 9/3 起样本全为议题二 marketable 生效后 (post-fix cohort), "
              "旧被动限价行为无实时样本 — 历史 13 条 passive 事件可由 trade_records "
              "._be_close_telemetry + data/replay_bars 重建 (须打 reconstructed=true 并与"
              "实时行分段判分, 属另案, 见 REVIEW-20260903 §五)。"
              " [2026-09-12 根因修] 9/11 重试风暴一夜灌 786 行重复 (平仓失败每 ~10s 重触发埋点, "
              "无幂等守卫) → 已去重 (备份 .bak_storm_20260911) 并在本模块落 _pen_event_exists 守卫 "
              "(键=(symbol,inst_id,exit_reason) 保留首行=真实触发瞬间; 风暴备份 821 行按本键去重=40 "
              "与生产文件逐一对应实证键正确)。pipeline.py 埋点零改动 (该文件在他 agent 未提交批中, "
              "避纠缠)。契约钉 tests/test_penetration_shadow.py §7 三例 (26 passed)。"
              "生效=下次服务重启 (G-02 部署断层守卫周六安全窗自愈); 重启前若再风暴按本键去重可复原。",
        count_mode="where:type=penetration",
    ),

    # ── 2026-09-04 (MU V反核查落地): 出场后全天路径 — 补「出场后到收盘」测量缺口;
    #    同批补登已停采的 markout (stop-zero-consumer 决策留痕, 防将来重复建) ──
    ShadowItem(
        id="exit_disable_cf",
        name="禁用型退出规则反事实影子 (M2)",
        status=ShadowStatus.OBSERVING,
        description="2026-09-04 落地: 对 MFE_STALL_EXIT / DEAD_SIGNAL_EXIT 出场笔, "
                    "用当日 1min bar 回放「若该规则禁用」的后续链 (β紧SL→BE/dd→EOD收盘), "
                    "saved=cf-live 方向感知。MFE_STALL 已于 8/15 实盘禁用 (DEB-20260815), "
                    "其测量为禁用决策的事后验证; DEAD_SIGNAL 仍启用, 其测量为禁用候选证据。",
        data_path="data/shadow/exit_disable_cf.jsonl",
        min_n=50,
        graduation_criteria="每规则 n≥50 且 saved 中位>0.15% (费用线) 且持有更优率≥65% "
                            "→ 开 DEB 裁决禁用/参数放宽; 否则维持。",
        eval_script="scripts/exit_disable_cf_shadow.py",
        created="2026-09-04",
        last_evaluated="2026-09-13",
        verdict_history=[{"date": "2026-09-13", "verdict": "KEEP_SAMPLING",
                         "evidence": "判据实测(每规则 n≥50 ∧ saved中位>0.15% ∧ 持有更优率≥65%): "
                                     "MFE_STALL_EXIT n=31 **<50** (saved中位+0.331% ✓ / 持有更优 "
                                     "61.3% **<65%** ✗ 两关不过); DEAD_SIGNAL_EXIT n=30 **<50** "
                                     "(saved中位+0.213% ✓ / 持有更优 80.0% ✓ 仅 n 不够)。"
                                     "⇒ 两规则均 n<50, 不可裁。注意 MFE_STALL 持有更优率 61.3% "
                                     "低于门限且该规则 8/15 已实盘禁用 — 本项对它是事后验证, "
                                     "攒够 n 后若仍 <65% 应如实记录'禁用决策的反事实支持偏弱'。"},
                        ],
        notes="首日回补 58 笔 (MFE_STALL 31 / DEAD_SIGNAL 27)。9/4 post_exit_path 真实路径"
              "交叉验证一致: MFE_STALL saved中位+0.331% ≈ post gap 中位+0.32%/均值+0.33%; "
              "DEAD_SIGNAL saved中位+0.217% vs post gap 中位+0.18%/均值+0.34%。"
              "DEB-20260904-dead-signal-exit 裁决提案已立案 (三路互证误杀)。"
              " [2026-09-12 补挂] 9/4 落地后从未挂 cron → 末行停 9/3 (停更8天, 9/11 体检发现, "
              "手工补跑 +3 笔累计 61: DEAD_SIGNAL 持有更优率 80%/saved中位+0.213%)。"
              "已挂 25 6 * * 2-6 (与 ssr 同班), 台账 crontab-exit-disable-cf 行同批入账, "
              "挂载哨兵 check_mounting 悬空观察随之消除。",
        count_mode="where:type=cf",
    ),

    ShadowItem(
        id="post_exit_path",
        name="出场后全天路径影子 (全量平仓单)",
        status=ShadowStatus.OBSERVING,
        description="2026-09-04 MU V反核查产物: 每笔 playbook 平仓后用当日 1 分 bar 记录"
                    "出场后路径 (post_fav/post_adv/close_gap + 日内深回撤V反上下文), "
                    "回答「出场后市场走成什么样」。边界 (防重复): 10m 短窗归 "
                    "penetration_shadow, 禁用反事实归 exit_disable_cf, 变体出场点归 "
                    "exit_param_shadow — 本项只做全量×全天纯路径记录。首日回补 60 天 "
                    "420 笔 (P1 口径重建后: ok 415 / ah_exit 5, 零截断零误标)。",
        data_path="data/shadow/post_exit_path.jsonl",
        min_n=100,
        graduation_criteria="无毕业门 (永久测量项): 服务退出判分对照臂 + 股性素材。"
                            "首次产出物: ①深回撤V反日 vs 平静日的 close_gap 分层是否显著 "
                            "(n≥100 后呈 DEB); ②exit_dist_from_low_pct 低×deep_dd_day "
                            "交集 (坑底附近割) 的错过分布 — 为保本线穿透判别器提供"
                            "下游路径佐证。",
        eval_script="scripts/post_exit_path_shadow.py",
        created="2026-09-04",
        notes="cron 05:45 ET (position_path_collector 05:15 → archetype_bars_ensure "
              "05:35 之后); 幂等 trade_id 去重; 状态四分类 ok/ah_exit/bar_truncated/no_bars "
              "(P1 口径 2026-09-04 晚: replay_bars 多写入者混杂目录, 截断 bar 自主补拉全天写回, "
              "台账 crontab-post-exit-path 条); 夜盘出场走 ah_exit + next_open_gap 隔夜量化。",
        no_adjudication_reason="PERMANENT_MEASURE: 判据字段自述'无毕业门 (永久测量项)' "
                               "(2026-09-21 清账 RFC-092) — min_n=100 只是呈报门槛, 不是裁决门; "
                               "原先机械迁移 READY 属误标 (无判据可跑)。服务退出判分对照臂, 继续采集。",
        count_mode="where:type=post_exit",
    ),
    ShadowItem(
        id="markout_capture",
        name="入场后 markout 漂移采集 (已停采)",
        status=ShadowStatus.KILLED,
        description="入场后 1/5/15min 方向感知 drift (sim 环境逆向选择代理)。"
                    "2026-08-29 用户裁决 stop-zero-consumer 停采: sim 阶段零消费者, "
                    "攒样无人判分 = 白采。累计样本止于 2026-08-28 (14 笔/日)。",
        data_path="data/shadow/markout.jsonl",
        created="2026-07-30",
        last_evaluated="2026-08-29",
        notes="补登 (2026-09-04): 8/29 停采时未入注册表, 状态变迁无审计痕 — 本次补录。"
              "与 post_exit_path (出场后全天路径) 语义不同: 本项测【入场后】逆向选择, "
              "彼项测【出场后】路径; 若未来 live 阶段需要重评估入场冲击成本, 按生命周期"
              "重启新条目而非复活本项。",
    ),
    # -- 2026-09-05 GLM A' 熔断影子 (DEB-20260904 §11.5 一期① + §11.6(a) 首跑证伪留痕) --
    ShadowItem(
        id="intraday_loss_breaker_a_prime",
        name="GLM A' 日内累计亏损熔断影子 (-400 预注册冻结)",
        status=ShadowStatus.OBSERVING,
        description="当日已实现 pnl_net 累计 <= -400 (exit 事件流口径, 阈值预注册冻结, "
                    "修改=重新裁决) 后, 记录当日余下新开仓为被挡: 信号清单+假想轨迹 "
                    "(=模拟盘真实结局)。record-only 零交易行为变更; 影子 != D1 落地, "
                    "是为解冻裁决攒证据。",
        data_path="data/shadow/cb_shadow_latest.json",
        min_n=30,
        graduation_criteria="判据字典 B18 (DEB-20260904 §11.5 预注册): 主判据=被挡笔期望 "
                            "(settled pnl_net 均值) <= -10 且 n>=30 -> 提请裁决启用实盘熔断 "
                            "(另立 DEB+用户裁); n>=30 顺延为毕业门槛。",
        eval_script="scripts/intraday_loss_breaker_shadow.py",
        created="2026-09-05",
        last_evaluated="2026-10-16",
        # 2026-10-16 (DEB-20261016 固化): status 维持 OBSERVING (采集继续), 但裁决已终态 ⇒
        # 挂账防 READY 误提升, 让所有按 status 取数的面读到「不启用」而非「攒样待裁」。
        no_adjudication_reason="ADJUDICATED_DO_NOT_ENABLE (2026-10-16, DEB-20261016): "
                               "预注册主判据符号相反 (现役期望 +23.84 vs 门 ≤−10) 且日聚类 "
                               "t=1.02<2 效应<MDE ⇒ 固化不启用; 保留 record-only 测量面",
        verdict_history=[{"date": "2026-09-13", "verdict": "KEEP_SAMPLING_FORWARD_ONLY",
                         "evidence": "判据实测(B18: 被挡笔期望≤-10 ∧ n≥30): n=194 ✓ 但期望 "
                                     "**+46.83** ✗ (被挡单净 +$9,084, 132亏/62盈)。"
                                     "⚠️ 该读数 **191/194 笔来自 backfill**, 而 notes 已记录 "
                                     "backfill 有前视偏差 (entry 序累加) 判不毕业; forward 样本"
                                     "仅 **3 笔** (9/8 单日) → 期望符号不可作裁决依据。"
                                     "根因: eval_script 自 9/5 创建起**从未挂 cron**, forward "
                                     "永远攒不动。2026-09-13 补挂 28 6 * * 6 (周跑, 全量重建幂等) "
                                     "+ 悬空告警随之消除。裁决须等 forward n≥30 后重测。"},
                         {"date": "2026-09-21", "verdict": "KEEP_SAMPLING_FORWARD_ONLY (重申)",
                          "evidence": "清账批 (RFC-092) 复核: 全期 n=204 被挡笔期望 **+45.0** (门 ≤-10, "
                                      "方向相反), 被挡笔累计净 **+$9,180**; last30d n=14 仍 +$146.75 "
                                      "(同号)。两口径同号说明'熔断会挡掉赚钱笔'的指向稳定, 但 forward "
                                      "样本 14 < 30 ⇒ 按 2026-09-13 已记录的规则 (backfill 有前视偏差, "
                                      "期望符号不可作裁决依据) **本次不裁**, 只叠加读数。"
                                      "结构建议留待 forward n≥30 重测时一并呈裁。"},
                         {"date": "2026-10-16", "verdict": "DO_NOT_ENABLE (固化, DEB-20261016)",
                          "evidence": "策略面 DEB 立项批闸门面复算: 全期期望 +43.78 的 88% "
                                     "(187/213 笔) 落在旧时代 (2026-04~06 backfill 段); A10 时代口径 "
                                     "(≥2026-07-01) 下 n=26 / 期望 +23.84 / 日聚类 t=1.02<2 且 "
                                     "效应<MDE80 ⇒ 「拦反」方向统计不可裁; 预注册主判据 (期望≤−10) "
                                     "符号相反 ⇒ 机械不呈裁。固化: 本门**不启用**, 保留 record-only "
                                     "测量面 (cb_shadow 已修 A10 口径, era_clean 与全期分列); "
                                     "若要测「回撤后均值回归」须另立 H1 前向预注册实验, "
                                     "不得拿本读数当实装依据。"}],
        notes="口径: trade_mode=simulate 且 pnl_net NOT NULL, ET 日, exit 事件流 "
              "(tie 时 exit 先结)。持久化=ftseek_us.db 派生表 cb_shadow_daily/"
              "cb_shadow_blocked (全量重建幂等) + JSON 汇总。首日回补即证伪预验证量级 "
              "(entry 序累加=前视偏差): 全期 n=191 期望 +46.90 主判据不过, 近30天 "
              "n=4 +254 方向相反 -> backfill 判不毕业, 价值转 forward 攒样; 详见 "
              "DEB-20260904 §11.6(a)。注册表计 n=全期 settled 被挡笔, 首日即 >=min_n "
              "属预期 (判据主门是期望非 n, READY 不等于毕业)。",
    ),
    # ── DEB-20261016 L3 (2026-10-16 用户裁「全批」): 高信心档倒挂守卫影子 ──
    ShadowItem(
        id="conf_tier_guard_shadow",
        name="高信心档倒挂守卫影子 (DEB-20261016 L3, record-only)",
        status=ShadowStatus.OBSERVING,
        description="逐日各 confidence 档 (精确值, 禁 ROUND 并桶) 滚动 20 交易日方向调整 "
                    "fwd_ret_t1 日聚类均值; 高档 (0.7) 持续负向+n≥30+|t|>2 连续≥5 观测日 ⇒ "
                    "标 CONSIDER_DOWNWEIGHT (只标记不执行, 供人工裁决)。定位=防尾部哨兵, "
                    "非择时器 (conf 近零 IC, RFC-124)。写方 scripts/conf_tier_guard_shadow.py, "
                    "cron 55 6 * * 2-6, 幂等按日覆盖。",
        data_path="data/shadow/conf_tier_guard.jsonl",
        config_key=None,
        min_n=30,
        eval_script="scripts/conf_tier_guard_shadow.py",
        eval_on_demand=True,
        created="2026-10-16",
        graduation_criteria="预注册 (DEB-20261016 §三.L3, 期间禁改): 标记门 = 高档滚动均值<0 "
                            "∧ |t|>2 (日聚类) ∧ 持续≥5 观测日 ∧ 窗内高档已决笔 n≥30 ⇒ "
                            "产「降权候选建议」供人工裁决 (任何权重调整=行为变更, 须另立 DEB+影子); "
                            "死线 2026-11-30 复核。n<30 只记录不标旗。",
        notes="2026-10-16 首跑: 近 20 交易日 0.7 档仅 n=1 (10 月信号 conf 分布移向 0.6, "
              "0.7 稀缺 — 与信心面归因「10 月 conf 值域变粗」互证) ⇒ 无旗, 静默正常。",
    ),
    # ── DEB-20261016 L1 (2026-10-16 用户裁「全批」): 隔夜 gap × SL 宽度假设影子 ──
    ShadowItem(
        id="overnight_gap_slcap_shadow",
        name="隔夜跳空×SL宽度上限影子 (slcap_4/slcap_5 变体, DEB-20261016 L1) (⛔已退役 2026-10-18)",
        status=ShadowStatus.KILLED,
        description="假设: 隔夜 gap 亏损 (10 月 LONG 4 笔 −$1296, 跳空 −5.6~−6.4%, SL 距离 "
                    "4.5-5% 被直接击穿) 与入场 SL 宽度相关 ⇒ 收窄上限存在可拦子集。"
                    "载体=退出回放引擎变体 slcap_4/slcap_5 (scripts/exit_replay_backtest.py, "
                    "初始 SL 宽度收窄至 4%/5%, 含赢家误杀面), record-only 零生产耦合。"
                    "前置保真门: validation.reason_match_pct ≥0.80, 未达则变体读数一律冻结 "
                    "(引擎自定门, DEB-20261014 已两次撞门)。",
        data_path="data/exit_replay_slcap_latest.json (variants.slcap_*)",
        config_key=None,
        min_n=30,
        eval_script="scripts/exit_replay_backtest.py",
        eval_on_demand=True,
        created="2026-10-16",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-18", "verdict": "FIDELITY_FAIL_REPLAY",
                          "evidence": "post-ca4487 窗口 10/09-10/16 重跑: 可回放 33 笔, "
                                      "reason_match=0.61 (<0.80 冻结点), 回放 baseline 净 -172~-151 "
                                      "vs 实盘 -495, 固有偏差 +322~+344; slcap_4/slcap_5 读数按 "
                                      "引擎自定门冻结, 不呈裁. 数据已刷新 (data/exit_replay_slcap_latest.json), "
                                      "但历史回放仍不可用于裁决; 下一步=前向 record-only 或退役."},
                         {"date": "2026-10-18", "verdict": "KILLED (用户裁退役)",
                          "evidence": "用户令「退役」: 历史回放 reason_match=0.606<0.80 不可裁决; "
                                      "无生产消费方; 数据保留不删; 重开=新预注册, 不许只翻开关."}],
        graduation_criteria="预注册 (DEB-20261016 §三.L1, 期间禁改): 样本门 = era 内隔夜仓 "
                            "n≥30 ∧ ≥10 交易日; 裁决轴 = slcap vs live_baseline 的净 bps 差 "
                            "(含赢家误杀面, 日聚类, t>2 且效应≥MDE80); 死线 ≈2026-11-30 或 n 达门。"
                            "前置: reason_match_pct≥0.80, 未达=读数冻结不呈裁。"
                            "判据4项全过 → 提请用户裁转生产 (改 SL 宽度=行为变更, 须另立 DEB)。",
        notes="2026-10-16 首跑 (post-ca4487 窗 n=29): **撞保真门 reason_match=0.586<0.80 "
              "⇒ 变体读数按引擎自定门冻结**, 不呈裁; 待锚定续放 (anchor 参数, DEB-20261014 "
              "v2 先例) 或攒样后重跑。引擎/载体无 bug, 是触发面时序重建保真不足。"
              "2026-10-18 用户裁退役: 重跑 n=33 reason_match=0.606<0.80, 历史回放路径穷尽; "
              "专用产物 data/exit_replay_slcap_latest.json 保留, 共享 exit_replay_latest.json 已恢复全窗; "
              "collection_retired=True 为账面退役标记 (载体本就按需, 无 cron); 重开须新预注册。",
    ),
    # ── KR3 预注册假设影子 (采集 9/2 上线 cron, **2026-09-06 补登记** — 未登记=无人跟踪) ──
    ShadowItem(
        id="kr3_direction_runup_gate",
        name="KR3 方向×涨幅证伪门 (RFC-072, 9 月唯一预注册假设) (⛔已退役 2026-10-15)",
        status=ShadowStatus.KILLED,
        description="H-KR3-1: 入场前同向已动 ≥1.0% (阈值冻结) 的指令, 净期望是否显著低于其余人群 "
                    "(=MFE≈0/SL_INITIAL 亏损族的事前可识别子集)。record-only **零拦截行为**; "
                    "采集器 scripts/kr3_runup_shadow.py 每日 06:10 (cron 2-6) 采上交易日入场, "
                    "trade_id 幂等去重。2026-09-06 补登 (修尺子批④, 用户授权代裁): 9/2 上线至 9/6 "
                    "**未在注册表** ⇒ 违「新增影子必须在此注册, 否则无人跟踪 = 等于没做」纪律, "
                    "OKR 面板只看得见产物、看不见攒样进度与离裁决门多远。",
        data_path="data/shadow/kr3_direction_runup_gate.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="RFC-072 §三 (预注册冻结, 期间禁改阈值): ①主判据 would-block vs 其余人群 "
                            "单均净期望差异**按日聚类**双样本 t>2 (pooled 不得单独采信, 宪章第 9 律) "
                            "②MDE 覆盖声称效应 (< MDE 判不可测, 不判确认) ③样本外: 窗口最后 2 周 vs "
                            "之前方向一致 ④次级端点: would-block 子集 MFE=0 率 ⑤样本量诚实: "
                            "would-block 子集 n<30 → **不裁决, 延长观察**。确认 → 10 月 OKR 立案为入场门 "
                            "(新 DEB + 用户裁 + 影子转正流程); 证伪 → 如实记录, **不立即换下一个假设** "
                            "(防链式 fishing, OKR-2026-09 KR3 明令)。",
        eval_script="scripts/kr3_runup_report.py",
        created="2026-09-02",
        last_evaluated="2026-10-15",
        collection_retired=True,
        intent="chase_high_ban",
        cross_ref=("futu_channel_top_movers",),
        verdict_history=[{"date": "2026-09-04", "verdict": "NO_ADJUDICATION",
                          "evidence": "首判 (data/shadow/kr3_runup_verdict_latest.json): n_collected=44 / "
                                     "n_eval=41; LONG|blocked n=15 mean +6.19 median +14.88 vs pass "
                                     "+20.88/+18.95; SHORT|blocked n=8 mean -25.42 median +11.2 vs pass "
                                     "+50.43/+25.0 → 双方向 DIRECTIONAL (期望与中位均更差)。"
                                     "**未裁决**: would-block 合计 n=23 < 30 (§三.5 样本门未达), 且均值受"
                                     "尾部支配 (SHORT blocked 总亏 -203 全靠 HPE -149/AVGO -60 两笔, "
                                     "tail_share 104.3%) — 一刀切全拦会误杀追涨多头 (LONG blocked 胜率 73%)。"},
                         {"date": "2026-09-13", "verdict": "NO_ADJUDICATION",
                          "evidence": "二判 (n_collected=84/n_eval=74, 未平仓待结算10): "
                                     "LONG|blocked n=21 **<30** mean +4.27 median +15.12 WR71% vs pass "
                                     "+15.44/+14.11 WR85% → **TAIL_ONLY** (中位不差, 全靠 worst2=-373 "
                                     "拖累, tail_share 416%) ⇒ 全拦会误杀多数; "
                                     "SHORT|blocked n=12 **<30** mean -22.29 median +7.21 vs pass "
                                     "+59.53/+18.72 → DIRECTIONAL 但样本远不足。"
                                     "**两腿 would-block 子集均 n<30 ⇒ §三.5 明令不裁决, 延长观察**。"
                                     "9/4→9/13 变化: LONG 由 DIRECTIONAL 转为 TAIL_ONLY (n 15→21 后"
                                     "中位翻正), 说明早读数是尾部噪声 — 印证样本门不可绕过。"
                                     "注: 判据①②③(日聚类t/MDE/OOS)按 RFC-072 §六.3 期末才现写, "
                                     "本次不出该三值属设计。"},
                         {"date": "2026-10-04", "verdict": "LONG=FALSIFIED / SHORT=INSUFFICIENT_SAMPLE",
                          "evidence": "期末裁决批 (scripts/kr3_final_adjudication.py, 现写现验, "
                                     "产物 data/shadow/kr3_final_adjudication.json): 窗口=预注册→9/30 "
                                     "入场且已结算 n=159, 阈值守卫 runup=1.0 未漂移。"
                                     "LONG|blocked n=43≥30: ①日聚类t=-0.02 (df=18.3) ②声称效应+2.10 < "
                                     "MDE80=32.88 ③OOS 前段-4.00 vs 后2周+10.36 **翻转** ⇒ 三要素 0/3 "
                                     "→ **干净证伪** (被拦组不更差, 门无 LONG 侧 edge; 稳健性: blocked "
                                     "median +15.4, worst2=-383.6 尾部占比 49.6% = 尾部风险形态而非方向错)。"
                                     "SHORT|blocked n=23<30 ⇒ §三.5 样本门未达 → **不裁决延长观察** "
                                     "(观测读数: t=-0.49 / 声称-39.02<MDE75.73 / OOS 同向一致-45→-43, "
                                     "尾部占比 86.4% — 方向线索存在但不可测, 只作记录不作结论)。"
                                     "处置 (OKR KR3 明令): 证伪如实记录, **不立即换假设**; LONG 腿证伪 + "
                                     "SHORT 腿样本门未达的最终处置 (记录/延长/停采/10月新假设) 属项目级, "
                                     "10 月开跑会用户裁决 (用户裁「按建议执行」2026-10-04 授权本判分批)。"},
                         {"date": "2026-10-15", "verdict": "SHORT=FALSIFIED ⇒ 两腿全证伪, KILLED "
                          "(用户裁「FALSIFIED 落档+停采」)",
                          "evidence": "SHORT 腿攒满样本门当日到期当日结算: would-block 已结算 n=30 "
                                     "(恰好 30/30; 另 1 行未结算系采集器 trade_id 幂等设计写入即跳过, "
                                     "非停采损失)。重跑同一裁决批 scripts/kr3_final_adjudication.py "
                                     "--window-end 2026-10-15 (B1 延长授权, 产物 window.authority 如实标注; "
                                     "判分 n=224/台账 261, 阈值守卫 runup=1.0 零漂移)。SHORT|blocked: "
                                     "①日聚类t=-0.2994 (df=28.0, 19拦簇/16过簇) 不过门 "
                                     "②声称效应-21.83 < MDE80=56.45 不可测 ③OOS 前段-44.98 vs 后2周-8.02 "
                                     "同向✓ ⇒ 三要素 1/3 → FALSIFIED 干净证伪 (方向线索一致为负但量级"
                                     "不显著且低于可测下限); LONG 腿延长窗复核仍 FALSIFIED (0/3), "
                                     "与 10/4 冻结窗结论一致。处置: 采集 cron `10 6 * * 2-6` 已注释留痕 "
                                     "(crontab-loop-baseline.txt 同步退役), jsonl 冻结留档不删; "
                                     "证伪不立即换假设 (KR3 明令); 重新开采=新假设须重新预注册。"},
                        ],
        notes="计数口径 = jsonl 行数 (trade_id 去重); **min_n=30 数的是 would-block 子集已结算笔数, "
              "不是总行数** ⇒ 注册表 READY 不等于可裁决, 裁决批须按子集 n 判 (2026-09-06 面板实测 "
              "子集 n=23/30)。判分代码 (按日聚类 t / MDE / OOS) 按 RFC-072 §六.3 **不得预先实现** — "
              "「期末裁决为独立批次, 判分口径现写现验, 防先写判分再凑数据的选择压力」; 故 eval_script "
              "只出稳健性视角 (均值/中位/胜率/尾部), 不出预注册判据值, 这是设计不是缺口。"
              "eval_on_demand=True: 报告脚本无 cron (手动/裁决日触发), 只有采集器挂 cron "
              "(crontab 10 6 * * 2-6, 台账 §2.3)。回滚 (2026-10-15 停采后) = 恢复 crontab 注释块中的 "
              "原 cron 行 + 本条目 revert KILLED; jsonl 冻结留档不删 (重新开采=新假设须重新预注册)。",
        eval_on_demand=True,
    ),
    # ── 2026-09-09 DEB-20260909 §5.2 (SL_INITIAL 根因 + 入场门重设计, 三模型辩论 5 轮,
    #    用户裁「同意」): D1'/D2' 两条 record-only 影子登记; B 案撤回, C 宽SL仅对照臂,
    #    D3' 监控折进 unit_cell_daily_readout by_source 小节 (无独立条目, 监控项无毕业判据) ──
    ShadowItem(
        id="extreme_gap_ban_shadow",
        name="D1' 极端跳空禁入影子 (DEB-20260909 §5.2)",
        status=ShadowStatus.OBSERVING,
        description="全日窗 (去开盘窗限定) × 双向 (aligned_gap_pct 方向归一 ⇒ LONG/SHORT 同判) × "
                    "双阈值并行腿 (ag≤−3 / ag≤−3.5, 顺向追高腿 ag≥+4 两腿共享) × 按源分层 "
                    "(DA/UE/REENTRY) 的「若禁入则不交易」反事实被拦篮读数。E1 原案被辩论证伪后 "
                    "(Aug-only 证据/9月误杀70%/−3 阈值悬崖/开盘窗只覆盖 48%) 的重设计。"
                    "数据源=既有 gap_alignment_shadow.jsonl 零新采集; 读数面=unit_cell_daily_readout "
                    "D1' 小节 (被拦 n/被拦净额/误杀率, 全历史判读+滚动20日 regime 对照)。",
        data_path="data/shadow/gap_alignment_shadow.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="DEB-20260909 §5.2 逐字: 每腿 n≥30 ∧ 被拦单反事实净<0 ∧ 误杀率≤40% "
                            "∧ 两腿结论同向; 任一不满足则降纯观测。达线只提请用户裁决, 不自动生效。",
        eval_script="scripts/unit_cell_daily_readout.py",
        created="2026-09-09",
        last_evaluated="2026-09-13",
        verdict_history=[{"date": "2026-09-13", "verdict": "NO_ADJUDICATION_FAIL_ON_FALSE_KILL",
                         "evidence": "判据实测(DEB-20260909 §5.2 逐字四关, 报告 "
                                     "data/reports/unit_cells_2026-09-13.md D1' 小节): "
                                     "腿「ag≤−3.0 或 ≥+4.0」被拦 n=58(门30)✓ 净−2029.06✓ "
                                     "误杀率 **50%**✗(门≤40%); 腿「ag≤−3.5 或 ≥+4.0」n=52✓ "
                                     "净−2010.29✓ 误杀率 **50%**✗; 两腿同向✓。"
                                     "⇒ **四关三过, 仅误杀率一关不过** ⇒ 未达毕业线, 维持纯观测。"
                                     "分源: DA 33笔−1485(误杀48%)/UE 18笔−532(56%)/REENTRY 7笔−12(43%) "
                                     "— 三源误杀率均 >40%, 非单源拖累, 无'只禁某源'的子集捷径。"
                                     "滚动20日 n=47 净−2035.75 与全历史几乎同值 ⇒ 出血集中近期但"
                                     "误杀率未改善, regime 漂移不改变结论。"
                                     "⚠️ 纠错留痕: 9/13 早先分析曾引'误杀76-87%/覆盖率5-14%', 那是按 "
                                     "mfe<0.05% 死单子集算的**另一口径**, 与本影子注册判据(被拦笔盈利占比)"
                                     "不可混用; 本行以注册判据口径 50% 为准。"},
                        ],
        notes="首日读数 (2026-09-09, 全历史 443 行/ag 缺失 99): −3.0腿 被拦 n=54 净−1947.13 "
              "误杀率48%; −3.5腿 n=48 净−1928.36 误杀率48% ⇒ 两腿同向但误杀率超 40% 门, "
              "**未达毕业线维持攒样**。计数口径注意: 注册表 current_n 是共享 jsonl 行数 "
              "(>min_n 属预期, READY≠毕业), 毕业 n 数的是**每腿被拦子集** (裁决时以当日报告 "
              "D1' 小节冻结值为准, 同 kr3 子集计数先例)。data_path 与 gap_alignment 影子共享 "
              "= 心跳由 52 5 cron 保证。回滚=删本条目 (读数小节属 unit_cell 脚本, 另案)。"
              "🔗 2026-09-26 RFC-109⑥ 标同轴: 与 gap_alignment 共用 data_path 且 n 完全相同 "
              "(524/30) ⇒ 同一批笔挂两个判据, **不得独立转正**; 归属: 本条=禁入提案 (D1'), "
              "gap_alignment=分源切片观测 (不另立门), 执行侧门=gap_chase_gate (B1)。",
        intent="gap_direction_alignment",
        cross_ref=("gap_alignment", "gap_chase_gate"),
    ),
    ShadowItem(
        id="entry_delay_confirm",
        name="D2' 触发延迟确认影子 (DEB-20260909 §5.2, 最高优先级)",
        status=ShadowStatus.OBSERVING,
        description="对每笔 PLAYBOOK_SIM 已平仓穿越触发单回放当日 1 分 bar: 「延迟 N∈{2,5}min "
                    "复核, 价格仍站触发方向则进场否则放弃」反事实。回避率 (延迟后不再触发) + "
                    "机会成本 (方向归一延迟价差, 只在仍触发笔上计) + 回避价值 (=−被回避笔 pnl_net)。"
                    "动机=SL_INITIAL 出血族尸检 (19 笔 16 笔 MFE=0 / 11 笔 ≤25min)。"
                    "口径: price_at(t)=首根 bar_time≥t 的 bar 开盘价; bar 加载/补拉/写回复用 "
                    "post_exit_path_shadow 同一实现; 幂等键 (trade_id, delay_min)。",
        data_path="data/shadow/entry_delay_confirm.jsonl",
        config_key=None,
        min_n=20,
        graduation_criteria="DEB-20260909 §5.2 逐字: 回放 n≥20 ∧ 避免率≥30% ∧ 机会成本<0.3%; "
                            "不达标退役。人群注记 (DEB 未指定人群, 双人群并列呈裁不算挪门柱): "
                            "避免率主判读人群=SL_INITIAL 出血族, 机会成本在全体仍触发笔上计。",
        eval_script="scripts/entry_delay_confirm_shadow.py",
        created="2026-09-09",
        notes="首日回补 (2026-09-09, 60 天 408 笔×2 腿, ok 407/腿, 夜盘入场 1 笔正确分类 "
              "no_bar_at_target): N=2 ALL 回避 50% 回避价值+$1680 opp 0.1231% ⇒ 双人群均过线; "
              "N=5 opp 0.3306% 超 0.3% 门 ⇒ ALL 不过 / SL_INITIAL (回避 80.95%) 过 — "
              "**按 DEB 这是攒样起点不是裁决点**, forward 攒样后以裁决日 latest.json 冻结值为准。"
              "强制披露随汇总输出 (disclosures): ①confirm_gate 反向先验 +0.50%/笔 t=5.67 "
              "(口径差异=盘前注入趋势域 vs 开盘窗微结构域) ②E3 PULLBACK_LIMIT TTL=300s 互斥 "
              "(转正时二选一或 TTL 重议)。count_mode=where:status=ok ⇒ current_n 是两腿 ok 行 "
              "合计, 每腿≈一半 (腿级 n 以 latest.json 为准)。cron 58 5 * * 2-6 (台账 "
              "crontab-entry-delay-confirm)。回滚=删本条目+删 cron 行+删两个 data/shadow 产物。",
        count_mode="where:status=ok",
        # RFC-141 (2026-10-10): 与 live+holdout 前瞻环双向互引 (同轴不同形态 —
        #   本环=历史 bar 回放影子, entry_confirm_forward=live 前瞻; 判据互不替代,
        #   裁决日两边读数须并陈)。单向互引会被 check_intent_uniqueness 判红。
        cross_ref=("entry_confirm_forward",),
    ),

    ShadowItem(
        id="entry_confirm_forward",
        name="RFC-141 入场延迟确认门 SIM 实弹前瞻 (D2' forward, 2026-10-10 用户裁)",
        status=ShadowStatus.OBSERVING,
        description="触发后 120s 复核: 价格仍站 reprice 执行锚同侧→放行, 反转→ABANDONED 放弃 "
                    "(落 guardrail SKIP reason=entry_confirm_reversed 可追溯)。"
                    "载体=10s 退出快循环 sweep (非 180s 主循环 — 防静默落地 3-5min 未预注册变体, "
                    "DEB-20261009 §27 缺陷1)。锚冻结: pending 期间 defer_pending 拦在 reprice 之前。"
                    "旗标走 metadata_json.entry_confirm (不碰 execution_state broker 镜像状态机)。"
                    "重启孤儿自愈放行。",
        # 2026-10-22 n 轴对齐 (全链路复查 R3): 原 data_path=jsonl + count_mode=
        # where:arm=treatment 计「机制台帐行」(≈裁决轴 5 倍虚高: 105 vs 20) ⇒ 假 READY。
        # 裁决轴 = 「v2 起 SIM 真实成交笔数」, 由夜结 settle 写入 latest.json 的
        # adjudication_source.all.n (cron 37 6 * * 2-6 每日刷新, 挂载核对面=该产物)。
        # jsonl 仍是机制留痕 (判据④佐证), 但不再是计数锚。
        data_path="data/shadow/entry_confirm_forward_latest.json",
        config_key="playbook.execution_parameters.entry_confirm_enabled",
        # 2026-10-10 用户裁改口径「直接走 sim 交易链路, 不用影子攒样看 sim 交易结果」:
        #   min_n 从 20 (影子样本) 改 60 (开门后 SIM 真实成交笔数), 与 RFC-141 §4 样本门一致,
        #   否则周检会用错的 n 驱动「到期未达标=治理缺口」告警。
        min_n=60,
        graduation_criteria="RFC-141 §4 逐字 v2 (2026-10-10 22:37 读数前预注册; 窗口按用户裁 "
                            "「从8月份开始统计, 早期的策略和逻辑不够成熟」改 8/1–10/9, 零读数下改窗 "
                            "= 修口径非挪门柱): "
                            "裁决源=开门后 SIM 真实盈亏 (trade_records, source_type=PLAYBOOK_SIM) "
                            "vs 门前基线, **不是**影子产物配对。样本门 n≥60 ∨ ≥10 交易日。"
                            "①病灶族收敛: SL_INITIAL 族净均 ≥−$40/笔 (v2 基线 −$162.3363; −$40 刻意"
                            "不随 v2 重算, 因 25% 收敛线 −$40.58 比 −$40 松 ⇒ 取严) "
                            "或该族 n≤1 ②开盘窗改善: 09:35–09:45 净均 >−$2.2632 ∧ 日聚类 t >−0.3218 "
                            "(t 门槛钉 v1 严值, 因 v2 基线 t=−0.3849 更负, 跟基线走会放松=挪门柱) "
                            "③机制量: 避免率≥30% ∧ CONFIRMED opp_cost<0.3% ④不伤赢家: 非 SL_INITIAL "
                            "族净均 ≥+$7.79 (v2 基线 +$9.7353 的 80%, 比 v1 地板 +$2.79 抬高 2.8×); "
                            "两端同尺 ⇒ 裁决用剔费口径 ≥+$8.79 (v2 基线 +$10.9854 的 80%, 剔 "
                            "fee_bps>100 的 2 笔), 全量口径只并陈。分层 R1(SHORT×开盘窗)/LONG×开盘窗"
                            "只印不判。jsonl 降级为机制留痕 (只证明门真在拦+拦的是反转单), 不独立裁决; "
                            "历史回放 580 行不混入。裁决三态 PASS/OBSERVE/FAIL 见 RFC §4; "
                            "PASS 后 REAL 转正另须 RFC 修订+用户签字, 不自动。",
        eval_script="scripts/settle_entry_confirm_forward.py",
        created="2026-10-10",
        notes="形态=SIM 实弹全量 (holdout_pct=0, 2026-10-10 用户裁: 留对照臂会稀释 SIM 结果)。"
              "强制披露: ①confirm_gate 反向先验 +0.50%/笔 t=5.67 (域不同, 盘前注入 vs 开盘窗微结构) "
              "②ALL 人群历史回放平 (t=−0.16/误杀58%), 达标证据全在 SL_INITIAL 事后族 "
              "(基线 29 笔 −$4,579.30 = 全样本净亏 1.8482×, 胜率 0/29, t=−10.4217), live 门无法事前"
              "只对该族开 ⇒ 前瞻 ALL 可能同样平 = 实验真实风险 ③无同期对照臂 ⇒ 开门后 vs 开门前"
              "受市况变化混淆, 缓解=判据1/3 是门自身机制量不随市况漂 (RFC §5.4) ④E3 PULLBACK_LIMIT "
              "TTL 互斥当前不存在 (entry_archetype_routing_enabled=false), 启用限价路由前须先裁。"
              "⑤键名与既有 entry_confirm_max_wait_s (下单后确认成交 30s 窗, playbook_service.py:1851) "
              "无关, grep entry_confirm 同时命中两者, 改动须看清语义。"
              "cron 37 6 * * 2-6 (ET 18:37 盘后结算, 台账 crontab-entry-confirm-forward)。"
              "回滚=entry_confirm_enabled:false+restart (孤儿自愈零卡单)+删本条目+删 cron 行; 产物留档。"
              "⑥v2 判尺修订 (2026-10-15 用户裁, RFC-141 §5.10): mid-mid 同尺+grace 2-of-3 "
              "(v1 混尺单点 54 行实证: 弃单率 61% 含 INTC+1.6bp 噪声杀, CONFIRMED opp 中位"
              "+0.36% 超判据③轨迹); schema 升 v2, 裁决窗自 v2 首行 date 重计 (两源不混算), "
              "判据阈值不变; 与 §5.9「多探测无 edge」的张力已并陈 (回放测旧混尺, 交互未覆盖)。"
              "回滚 v1=ruler:legacy+grace_sweeps:0+restart。",
        count_mode="json_key:adjudication_source.all.n",
        cross_ref=("entry_delay_confirm",),
    ),
    ShadowItem(
        id="exit_t_arm_min_dd_08",
        name="RFC-154 T 臂: adaptive trailing min DD 0.4→0.8 (只读回放)",
        status=ShadowStatus.OBSERVING,
        description="只读全路径回放: 同一批 PLAYBOOK_SIM 历史交易, baseline adaptive min DD 0.4 "
                    "vs T 臂 0.8; 输出扣费净 bps、delta、SL_INITIAL、误杀/避免、日聚类 t/MDE。"
                    "零交易行为变更; 不改 exit hash/生产参数。",
        data_path="data/shadow/exit_t_arm_latest.json",
        config_key=None,
        min_n=30,
        count_mode="json_key:sample.n",
        graduation_criteria="预注册见 RFC-154 §2.2: 全路径入场→出场重放 (pess/opt 双序); "
                            "样本门 ≥10 ET 日 ∧ ≥30 笔; 提案门 T−baseline 扣费净 bps 均值 ≥+10bps "
                            "∧ 日聚类 t≥2.0 ∧ SL_INITIAL 不恶化 ∧ 赢家误杀净额 < 亏损避免净额; "
                            "reason_match<0.80 时按引擎纪律冻结变体读数。",
        eval_script="scripts/exit_t_arm_shadow.py",
        eval_on_demand=True,
        stale_expected=True,
        no_adjudication_reason="FIDELITY_GATE: 2026-10-20 首跑 reason_match=0.604<0.80 "
                              "(引擎冻结门); T 臂 delta 读数不可采信, 不提案; 待锚定回放/"
                              "更高保真数据后重跑.",
        created="2026-10-20",
        verdict_history=[
            {"date": "2026-10-20", "verdict": "FROZEN_BY_FIDELITY",
             "evidence": "139 笔/21 日; pess delta −5.77bps (t=−1.3), opt +0.04bps (t=0.44); "
                         "harm 1139bps / save 0; reason_match=0.604<0.80 ⇒ 按回放引擎纪律"
                         "（scripts/exit_replay_backtest.py 文档线 40）冻结变体读数为不可采信。"
                         "结论=不改生产 min DD; P0 的出场后 trail +9bps 不能直接外推为全路径改进。"},
        ],
        notes="2026-10-20 用户确认开始 T 臂影子。首跑只读缓存 bar (139/139 可回放, 0 缺 bar), "
              "未触 OpenD。关键教训: P0 出场后反事实 (从实际出场后继续持有) 与全路径回放相反 — "
              "T 臂把 0.4→0.8 后, 持有期回吐增加, pess 序 harm 1139bps、save 0。"
              "fidelity 0.604 的原因待查 (1m 粒度/BE 限价成交/费用垫口径), 在解决前不读变体。"
              "回滚: 删本条目/脚本, 零生产影响。",
    ),
    ShadowItem(
        id="risk_size_tier_shadow",
        name="RFC-155 左尾风险尺寸分层影子 (只读反事实)",
        status=ShadowStatus.OBSERVING,
        description="对已平 PLAYBOOK_SIM 交易按风险旗标缩放仓位 (只读): "
                    "R1 ATR>=5%→0.5x (主) / R2 开盘0-15min / R3 UE / R4 上述>=2旗标 / "
                    "R5 any / R6 每旗标0.5x下限0.25x。比较美元净额、日级回撤、左尾净额、"
                    "避免亏损 vs 放弃赢家。不改单笔 bps/入场信号; 生产零改动。",
        data_path="data/shadow/risk_size_tier_latest.json",
        config_key=None,
        min_n=30,
        count_mode="json_key:sample.n",
        graduation_criteria="预注册见 RFC-155 §3: 历史反事实 + 前后半窗拆分; "
                            "转生产前须前瞻影子 (只记录 would_multiplier/counterfactual_size) "
                            "≥10 ET 日且 ≥30 笔受影响交易; 缩放下净额/回撤不劣于实际且左尾净额下降, "
                            "放弃赢家成本 < 避免亏损才采纳; 生产实现为风险乘数, 一行回滚。",
        eval_script="scripts/risk_size_tier_shadow.py",
        eval_on_demand=True,
        stale_expected=True,
        no_adjudication_reason="IN_SAMPLE_COUNTERFACTUAL: 2026-10-20 首跑仅历史反事实; "
                              "转生产须前瞻影子 (只记录 would_multiplier) + RFC + 用户批准.",
        created="2026-10-20",
        verdict_history=[
            {"date": "2026-10-20", "verdict": "READOUT_IN_SAMPLE",
             "evidence": "155 笔/22 日: R1 atr5 delta +$855.54 (DD 1057.63→656.81; "
                         "半窗 +229.10/+626.43); R4 ge2 delta +$1,065.70 (DD→534.11; "
                         "半窗 +673.04/+392.67); R6 product +$1,191.86 (DD→258.82, 但影响 123/155 笔); "
                         "R2 open15 +$587.84 (半窗 +490.69/+97.15); R3 ue +$386.95 (半窗 +386.95/0)。"
                         "结论=预注册主臂 R1 稳定正向; R4 更强但为组合档, 需前瞻; 均不直接切生产。"},
        ],
        notes="2026-10-20 用户确认继续 P2。首跑只读 DB(mode=ro); 费用按净额线性缩放, "
              "固定佣金非线性未建模。尺寸缩放只改变美元暴露/回撤, 不改变单笔 bps/胜率, "
              "不得宣称 alpha 改善, 不得借机加仓/提频。"
              "R4 的 ge2 = >=2 of (ATR>=5%, 开盘0-15min, UE), 与左尾尸检表里含 plan-gap 的"
              "“>=2 flags”不是同一旗标集, 不可混读。回滚: 删本条目/脚本, 零生产影响。",
    ),
    ShadowItem(
        id="risk_size_tier_forward",
        name="RFC-155 P2 风险尺寸前瞻记录影子",
        status=ShadowStatus.OBSERVING,
        description="每条真正走到 execute_instruction 的指令, 只记录风险旗标/六种 "
                    "would_multiplier/counterfactual_size 与实际执行结果; "
                    "**不改实际 size/入场/退出**。用于前瞻验证 R1/R4 尺寸分层。",
        data_path="data/shadow/risk_size_tier_forward.jsonl",
        config_key="playbook.execution_parameters.risk_size_shadow_enabled",
        min_n=30,
        # 2026-10-22 n 轴对齐 (全链路复查 R3): 原 count_mode=where:success=True 计「成功执行
        # 行」(≈判据轴 3 倍虚高, 会在真实样本门未达时假 READY); 判据轴 = RFC-155 §3.2
        # 「≥30 笔受影响交易 (≥1 旗标真)」。写方 build_row 自本日起落顶层 affected 布尔
        # (嵌套 flags 无法被 where: 表达) ⇒ 本计数自 2026-10-22 字段上线起算; 此前
        # 9 行中 3 行受影响 (人工复算), 缺口 3 笔属保守方向 (延迟 READY, 不伪造)。
        count_mode="where:affected=True",
        graduation_criteria="预注册见 RFC-155 §3.2: ≥10 ET 日 ∧ ≥30 笔受影响交易 "
                            "(≥1 风险旗标为真 ⇒ 会被缩放; 不是全部已执行笔数 — "
                            "2026-10-21 评估器实现对齐 RFC 原文, 此前误用 sample.n); "
                            "缩放下净额/回撤不劣于实际且左尾净额下降; "
                            "放弃赢家成本 < 避免亏损; 转生产须 RFC + 用户批准。",
        eval_script="scripts/risk_size_tier_forward_eval.py",
        eval_on_demand=True,
        created="2026-10-20",
        verdict_history=[
            {"date": "2026-10-20", "verdict": "COLLECTING_STARTED",
             "evidence": "用户确认实施前瞻记录影子; 只记录 would_multiplier, 不改实际 size。"
                         "历史反事实 R1 +$855.54 / R4 +$1,065.70 仅作方向证据。"},
        ],
        notes="记录器在 execute 前构建、execute 后收口; 成功行清旧 skip_reason 到 "
              "prior_skip_reason。数据从下次重启后的执行尝试开始; 评估器只读 join "
              "trade_records。回滚: risk_size_shadow_enabled=false + 重启, 数据保留。",
    ),

    # ── 2026-09-11 断头传感器修复批 (用户裁「做」): 两个有写者无读者/无登记的影子补全判分侧 ──
    ShadowItem(
        id="premarket_invalidation",
        name="盘前失效评估影子 (B 规则; A/C 2026-09-11 退役)",
        status=ShadowStatus.OBSERVING,
        description="每日 09:35-09:59 ET 对当日可执行指令按涨跌幅 (vs snapshot 昨收) 打标, "
                    "record-only。8/11-9/10 攒 310 行零结算 (无结算器无台账 = 8/28 静默零产出"
                    "同款), 9/11 补 settle_premarket_invalidation.py 判分侧。同日 DEB-20260911 "
                    "两轮多模型辩论裁退役 A_short_chase/C_long_chase 采集 (仅存 B_long_knife), "
                    "历史行不删。",
        data_path="data/premarket_invalidation_shadow.jsonl",
        config_key="playbook.premarket_invalidation_observe",
        min_n=20,
        graduation_criteria="预注册 (settle 脚本头, 2026-09-11 写死于裁决读数前): 每规则 "
                            "executed n>=20 ∧ 禁入价值>0 (executed 净<0) ∧ 误杀率<=40% ∧ "
                            "全窗与滚动20交易日同向 → 提请转正另裁。B 规则注记: 执行时刻口径"
                            "硬门已在线 (RFC-068), 判据针对盘前口径增量。【口径更正·2026-09-21 用户裁"
                            "「口径随实现」: 前句'盘前口径增量'更正为'开盘后早段盘中 (09:35–09:59 ET)"
                            "口径增量'——B 规则判开盘后跌>5%事件, 盘前无该事件相位; 实测产物 396 条"
                            "全落 09:35–09:47 ET (RFC-092f §3)。B 已攒样均为盘中采集, 更正后仍有效,"
                            "不清零; '盘前数据独立增量'如要问须另立预注册。】首读 (9/11): "
                            "C 净-$152 但误杀 68% 超门 / B n=3 不足 / A n=1 — 全部 KEEP_SAMPLING。"
                            "**A/C 已退役 (DEB-20260911): 结算器恒判 RETIRED_NO_PROMOTION, "
                            "历史行仍结算呈报但永不提请转正。**",
        eval_script="scripts/settle_premarket_invalidation.py",
        created="2026-08-09",
        notes="count_mode=where_nonempty:would_invalidate (flagged 口径, 与结算器分母一致)。"
              "cron 2 6 * * 2-6。回滚=删 cron 行+删结算器+删本条目 (采集侧 observe 开关独立)。"
              "**口径差提示**: 本条 current_n 是 flagged **行数** (9/11 时 33 行 ≥ min_n=20 ⇒ "
              "周检会报 READY_TO_EVALUATE), 而转正判据是**每规则 executed n≥20** — 通知不可"
              "直接读成「可裁决」, 以结算器 latest.json 的 per-rule verdict 为准。"
              "A 的假设「逆势追空差」去向: 并入 short_chase_ban_shadow (触发时刻相位) 单一"
              "证据源重测, 快照行不得独立转正 (DEB-20260911 C8)。",
        count_mode="where_nonempty:would_invalidate",
        intent="long_knife_gate",
        cross_ref=("short_chase_ban_shadow",),
    ),
    # ── 2026-09-11 DEB-20260911 P0-2 补登记 (用户裁「按建议执行」): 8/8 起攒样但
    #    registry/watchdog/台账三处零登记 —「建了没登」第 4 例 (前三例见台账 §2.4 入账提醒)。
    #    9/11 起本项是「逆势追空禁入」意图的**单一证据源** (A_short_chase 快照规则已退役,
    #    其假设并入本项, 触发时刻相位 = B 转正先例 RFC-068)。──
    ShadowItem(
        id="short_chase_ban_shadow",
        name="SHORT 逆势追空禁入影子 (触发时刻口径) (⛔已退役 2026-10-19)",
        status=ShadowStatus.KILLED,
        description="DEB-20260808 §8.3 裁定①: 「SHORT 且当日涨>3% 禁入」硬门在 "
                    "post-blackout(1015) 基线残存效应 −$19.80 被否 → 仅 shadow 记录。"
                    "写者 pipeline.py (SHORT 触发评估处, ctx.change_percent 过阈值 → 命中行; "
                    "任意值 → 每「指令,ET日」首轮扫描行), 含 triggered/entry_path。8/15 禁触窗 "
                    "1015→0935 (unified_config.yaml:1127, git 1d2b909d) ⇒ 旧否决前提失效, "
                    "本影子 = 新基线下重测该假设的唯一载体 (2026-09-11 起, DEB-20260911)。"
                    "**2026-09-18 采样设计修复 (用户令「按建议」)**: 原写者只在 chg>阈值 时落行 "
                    "⇒ 8/24-9/18 命中仅 9 条指令 (配到成交 6 笔), 10/31 死线按节奏到不了 n≥20, "
                    "判据永不可裁且无对照臂; 现同文件增写**扫描行** (over_threshold=false, "
                    "每「指令,ET日」首轮 1 行) 作对照组, 命中行语义逐字不变。",
        data_path="data/short_chase_ban_shadow.jsonl",
        count_mode="distinct_where:over_threshold=True",
        config_key="playbook.execution_parameters.short_chase_ban_shadow_enabled",
        min_n=20,
        graduation_criteria="预注册 (2026-09-09 用户裁, handoff 9/9 批/REVIEW-20260909 §I-8): "
                            "n>=20 ∧ 反事实方向一致>=70% → 提案硬门复活 (另立 DEB + 用户裁); "
                            "**死线 2026-10-31**, 到点不达标 → 裁退役或降纯日志。"
                            "转正前置 (DEB-20260911 C8): 昨收分母分裂修复落地 + 污染期样本"
                            "处置已裁, 否则到点裁的是废样。"
                            "(n 口径 = distinct instructions, count_mode=distinct_where:over_threshold=True; "
                            "扫描行不计入; 判据须用扫描行作对照臂: 命中组 vs 未命中组净额差 + 方向"
                            "一致率, 单看命中组无 IC 意义 — 2026-09-18 补; "
                            "2026-10-18 复核: W7 分母分裂未修复 (divergent_n=128), 当前样本污染, 10/31 前须先裁分母或退役)",
        eval_script=None,
        collection_retired=True,
        created="2026-08-08",
        verdict_history=[
            {"date": "2026-08-08", "verdict": "HARD_GATE_REJECTED_OLD_BASELINE",
             "evidence": "DEB-20260808 §8.3: post-blackout(1015) 基线残存效应 −$19.80 "
                         "(8月逆势亏单全在9点档, 被禁触窗架空) → 不落硬门, 仅 shadow 记录"},
            {"date": "2026-09-11", "verdict": "SSOT_FOR_SHORT_CHASE_INTENT",
             "evidence": "DEB-20260911: 禁触窗 8/15 已改 0935 ⇒ 旧否决前提失效, 假设在新基线"
                         "下未检验; A_short_chase 快照规则退役, 假设并入本项作单一证据源 "
                         "(A 的 310 行 IC: SELL_SHORT rho=-0.378 p≈0.005 方向成立, 但载体"
                         "相位须为触发时刻); A 快照行**不得独立转正**"},
            {"date": "2026-10-18", "verdict": "BLOCKED_DENOMINATOR_SPLIT",
             "evidence": "W7 2026-10-16 读数 divergent_n=128; 命中仅 13 distinct inst <20 "
                         "(227 行 / 8 笔匹配净 -$17.3); 扫描 85 行 / 62 inst; 分母分裂未修, "
                         "10/31 样本按污染期处理 ⇒ 不建结算器, 先裁分母或到期退役."},
            {"date": "2026-10-19", "verdict": "KILLED_REASONABLENESS",
             "evidence": "用户裁「退役, 基于合理性」: W7 分母分裂自 9/11 未修 (10/16 divergent_n=128), "
                         "命中仅 13 distinct inst <20; 继续采集只会产出污染样, 10/31 无法有效裁决; "
                         "旧基线硬门已否决一次 (DEB-20260808 §8.3). 数据保留; 重开须先修分母 + 新预注册."}],
        notes="**污染期标注 (DEB-20260911 P0-2)**: chg 参照 ctx.change_percent (futu_quote, "
              "market_data_router.py:432), 与 premarket_invalidation 快照侧 get_snapshot 的 "
              "prev_close 分母已实锤分裂 — BIDU 9/8 同指令 7 秒内 −7.65% vs +4.07% (隐含分母 "
              "99.47 vs ≈88.26)。分母修复落地前的样本为污染期, 是否计入 10/31 预注册 n 由"
              "裁决日显式裁, 不得默认全计。join 键警示: inst_id 有脏数据 (NTES 8/24 "
              "inst_id=1535 同日 DA BUY / reentry SHORT 双方向), 结算须 symbol+date+direction "
              "复核, 不能单靠 inst_id。**采集濒死实证 (9/11 复核)**: 8/24-9/10 仅 5 行 "
              "(NTES 8/24、MSTR 9/1、DDOG 9/3、BIDU 9/8 ×2) — 按此节奏 10/31 死线大概率裁 "
              "KILLED 而非达标; 且行内无 date 字段 (日期只能从 ts 截)、direction 在写者处硬编码 "
              "'SHORT' (非取自指令), 这两项裁决前须先补。eval_on_demand=True: 无独立结算器, "
              "裁决日 (或 10/31 死线) 手工结算; n 计数锚 = 命中行 (count_mode="
              "where:over_threshold=True, 2026-09-18 起)。"
              "**2026-09-18 采样设计修复落地 (用户令「按建议」)**: ①写者改为「过阈值→命中行 "
              "(逐轮, 语义不变) + 任意值→扫描行 (每指令每日首轮 1 行, over_threshold=false)」, "
              "扫描行=对照组, 使「命中组 vs 未命中组」净额差可算 (原设计只有命中组 = 无 IC)。"
              "②存量 121 行原子重写补 date/decision_id/over_threshold=true/scan=false 四字段"
              "(值未改, 备份 .bak-20260918), 121/121 行 decision_id 可解。③date 字段与 "
              "decision_id 补齐 ⇒ notes 中「join 键脏数据/无 date」两项告警解除 (仍建议 "
              "symbol+date+direction 复核)。direction 仍硬编码 'SHORT' (写者处仅 SHORT 分支, "
              "无歧义)。"
              "**不入 data_freshness_watchdog (9/11 裁)**: 本影子写盘极稀 (18 天 5 行), 小时级"
              "新鲜度阈值会永久误报; 断流由 check_mounting 的 14 天静默哨兵接管。台账行另批补 "
              "(任务 #9)。回滚 = short_chase_ban_shadow_enabled 置 false。"
              "2026-10-19 用户裁退役 (基于合理性): config 键已置 false 真停采; 数据保留; "
              "重开须先修分母 + 新预注册; 旧基线硬门否决史见 verdict_history。",
        eval_on_demand=True,
        intent="short_chase_ban",
        cross_ref=("premarket_invalidation",),
    ),
    ShadowItem(
        id="ue_tier_gate",
        name="UE tier 闸影子 (PROPOSAL-20260905 U3/P4, 9/11 补结算器) (⛔已退役 2026-10-19)",
        status=ShadowStatus.KILLED,
        description="HARD_EVENT 直进 / MARKET_CORROBORATED 需补强 / THESIS_ONLY 拦 — 影子判定"
                    "落盘 (写者 llm_universe_scan._log_tier_gate_shadow), enforce=false。"
                    "9/11 补 ue_tier_gate_shadow_eval.py: 被拦/放行桶 fwd_return_t1 对照 + "
                    "LONG/SHORT 方向分层并列读数 (UE LONG 全史净-$780 vs SHORT +$208 的裁决备证)。",
        data_path="data/shadow/ue_tier_gate_shadow.jsonl",
        config_key="universe_expansion.tier_gate_shadow",
        min_n=15,
        graduation_criteria="预注册 (unified_config.yaml 逐字, 2026-09-05): 攒样>=15交易日 ∧ "
                            "被影子化桶 fwd 回填 n>=30; 毕业=被拦桶方向性前瞻均值<=0 ∧ 放行桶"
                            "均笔>0 → 用户裁决; 撤闸认错=被拦桶前瞻>+0.3% ∧ n>=30。"
                            "方向分层只并列呈报不另设门 (防多重比较挪门柱)。",
        eval_script="scripts/ue_tier_gate_shadow_eval.py",
        created="2026-09-05",
        last_evaluated="2026-10-19",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-19", "verdict": "KILLED_REASONABLENESS",
                          "evidence": "写者仅挂 UE inject; 10/18 DA 主链改造后 scan --da-feed "
                                      "skip inject ⇒ 10/05 后断供; 10/19 scan 仍 surfaced 7 但 "
                                      "无 tier-gate 行; 被拦 fwd n=4<30; eval cron 已摘; "
                                      "数据保留; 重开=新预注册."}],
        notes="count_mode=distinct:date (每扫描日 1 行, 行数=交易日数, 与 >=15 交易日判据同轴)。"
              "cron 52 5 * * 6 (fwd 回填 05:30 后, 与 ue_tier_shadow_eval 05:50 同窗)。"
              "与 tier_gate_enabled 互锁: 影子期禁止打开 (config 注释在案)。回滚=删 cron 行+结算器+本条目。"
              " 🔵**通道级经济性备证 (2026-09-26 用户裁「按建议执行」= 建议1: 登记备证不立门, record-only)**: "
              "结算器新增 `channel_economics` 段 (**gating=False, 只印不判**, 不参与 verdict), 实测 UE 通道 "
              "n=86 (decision_id LIKE 'universe_expansion%' 归属口径) 毛利 −$40.38 / 费用 $524.88 / 净 −$565.27 ⇒ "
              "**毛利率 −1.02bps < 费率 13.25bps** = 费用归零仍不赚 ⇒ 结构性无毛利来源, 与用户 8/22 "
              "证伪「执行纪律可保底」同型 (那次费=毛利 42 倍, 本次更彻底: 毛利≈0)。"
              " 前瞻 t 检验 **LONG t=+0.109 / SHORT t=+0.874 ⇒ 两方向皆零 alpha** ⇒ 「禁 UE LONG / "
              "不对称限流」**无数据依据**, 且撞用户 8/2「市场双向, 反对不对称限流」与本判据 「方向分层"
              "只并列呈报不另设门」禁令 ⇒ 方向读数只作并列披露 (`by_direction_disclose_only`: LONG 毛利率 "
              "−22.70bps/净−$841 vs SHORT +30.57bps/净+$276)。"
              " **为何不立门**: ① UE 整体净亏 t=−0.827 **不显著** (n=86, 均−6.57, sd 73.72); ② 9月净仅 "
              "−$56.41 (7月−104/8月−404) 金额小且无趋势; ③ SHORT 腿是正 edge (+30.57bps), 停通道会砍掉 "
              "赚钱的腿。⇒ 病灶是「无毛利来源」不是「选错方向」; 而钱是退出机制赚的 (前瞻皆零 alpha 但实盘 "
              "毛利一负一正 ⇒ 差异在退出侧), 退出参数已被 DEB-20260819 终裁「近似最优, 执行层不再改」⇒ 该杠杆 "
              "合规上锁死。契约测试 tests/test_ue_tier_gate_channel_economics_contract.py 钉死只印不判 (7 passed, "
              "变异测试 4/4 种破坏被抓 ⇒ 守卫有牙齿)。"
              " ⚠️ 审计局限: `fees_est` 是估算列 (伴 fees_source) 未逐笔对账; tier 维度对实盘成交覆盖率 "
              "仅 ~24% (影子 9/08 起, 成交回溯 7/14) ⇒ 任何 tier×盈亏 结论都是小样本, 不得据此立判据; "
              "前瞻 vs 实盘相关 r=+0.403 (t=+4.01, n=85) ⇒ 尺子有效但不强 (符号一致率 63%)。"
              "2026-10-19 用户令按建议执行 ⇒ 退役 (KILLED): 写者只挂 UE inject 路径, "
              "10/18 DA 主链改造后 scan --da-feed 明确 skip inject ⇒ 影子断供; 数据止于 10/05; "
              "被拦 fwd n=4<30; eval cron 已摘; 数据保留; 重开=新预注册 + 恢复写入路径。",
        count_mode="distinct:date",
    ),
    # ── 2026-09-15 (DEB-20260910 §22-§25 报价新鲜度门, 用户裁「请执行」) ──
    ShadowItem(
        id="ue_quote_freshness_shadow",
        name="UE 报价新鲜度门「哑弹」量化影子",
        status=ShadowStatus.OBSERVING,
        description="纯只读探针 (scripts/ue_quote_freshness_shadow.py): 对当日 UE surfaced "
                    "标的, 用 **SSOT 自己的判据** (`evaluate_candidate`+`_source_time`) 算三假设"
                    "资格差异 — as_is(现状/门哑火) · alias(供应商 timestamp=我方抓取时刻) · "
                    "genuine(futu update_time=交易所侧) ⇒ 量化'激活 SSOT stale_quote 门'会新拒谁、"
                    "以及 alias 会洗白多少真陈旧。零生产路径改动 (不进 inject/scan、不落库、"
                    "不下单、不改配置、不动退出参数)。",
        data_path="data/shadow/ue_quote_freshness_shadow.jsonl",
        min_n=10,   # 单位 = 交易日**有效轮次** (count_mode=distinct:date), 不是行数
        graduation_criteria="预注册 (写死于台账 §2 `crontab-ue-quote-freshness-shadow` 行 + "
                            "crontab 注释, 本处不改判据): ≥10 个**交易日有效轮次** ∧ 盘前 genuine "
                            "龄 **P95 ≤ 候选阈值** → 可提议激活并定盘前专属阈值 (另立 DEB + 用户裁); "
                            "**有效轮次 <10 禁止建议激活** (同 thinking-eval 先例); P95 超阈值时"
                            "**不得事后改阈值重算** (改阈值 = 重新预注册)。",
        eval_script="scripts/ue_quote_freshness_shadow_eval.py",
        created="2026-09-15",
        count_mode="distinct:date",   # 与预注册单位同轴 (交易日轮次 ≠ jsonl 行数)
        last_evaluated="2026-10-09",
        verdict_history=[
            {"date": "2026-09-15", "verdict": "BASELINE_NO_DATA (挂环当日 1/10 轮次)",
             "evidence": "挂环当日 4 组读数 (ET 盘前同一批 5-7 标的): 交易所时戳可得率 5/5→7/7 "
                         "=100%; genuine 龄 1.5–159.1s (AXON 4 次观测 2 次超 90s ⇒ **90s 阈值在盘前"
                         "阵发报价下不成立**); alias 洗白真陈旧 1/5 (AXON 别名 −10s'很新鲜' / 交易所 "
                         "159.1s 已超阈)。机械裁决器首跑 = INSUFFICIENT_RECORD (有效轮次 1/10, "
                         "P95 41.8s 仅供参考) ⇒ 禁止建议激活。"},
            {"date": "2026-10-09", "verdict": "NOT_READY (18/10 轮次达标但判据不过门)",
             "evidence": "机械裁决器 18 个有效交易日轮次 (2026-09-15→10-08, 22 读数行, "
                         "0 坏行/forced/非交易日): genuine 龄 P50=9.7s / **P95=1329.2s** / "
                         "max=48010s ⇒ P95 >> 90s 候选阈值, 激活会**系统性误杀** "
                         "(超阈 30 次、会新拒 17 个候选、别名洗白 30、交易所时戳可得 123/130)。"
                         "按预注册: 不得事后改阈值重算, 维持现状 (9/15 用户裁 C); "
                         "要盘前专属阈值须另立 DEB 重新预注册。裁决逾期 11 天 "
                         "(9/28 达 10 轮未被触发), resume 会话补跑, 读数落 "
                         "data/shadow/ue_quote_freshness_shadow_eval_latest.json。"}],
        # 已裁决 NOT_READY: 样本量达标但判据不过门, 现状下无待裁事项; 挂账阻断
        # refresh_counts 运行时提升 (否则每周 READY 通知 = 已裁事项重复报数, 且
        # 全量套件里提升后与下方静态 OBSERVING 钉冲突)。重提激活 = 新阈值重新预注册。
        no_adjudication_reason="ADJUDICATED_NOT_READY (2026-10-09): P95=1329.2s >> 90s, "
                               "激活=系统性误杀; 采集继续作证据基, 新阈值须重新预注册 (另立 DEB)",
        notes="挂环 `crontab-ue-quote-freshness-shadow` (15 21 * * 1-5 = 09:15 ET 盘前, 刻意避开 "
              "21:10 scan→inject→push 链, 不抢 OpenD 槽位/DailyK 配额)。"
              "**2026-09-15 三点补口 (用户裁「请执行」)**: ①**交易日守卫** — 探针在取数**之前**"
              "判交易日 (纯本地、**零 futu 连接**: 假日历只读常量, 因为共享日历 accessor 实测会开 "
              "OpenQuoteContext 而 OpenD 槽位与生产共享), 假日/覆盖面外一律**不落盘** (`--force` "
              "人工行带 `forced_non_trading_day` 标记, 裁决器排除) — 因 cron 假日照跑而假日"
              "报价龄小时级会伪造'激活必杀'读数; ②**裁决器 + 到期触发人** — 本条注册 + "
              "eval 脚本 (机械只读, `eval_on_demand=True`), 补上「谁在 10 交易日后读这把尺子」"
              "(此前只活在 crontab 注释里, 到期无人被提醒); ③**alias 边界加红钉** — inject 侧喂"
              "抓取时刻不得判 stale (行为钉 + 门有牙阳性对照) + 扫描期归一函数只能被初筛调用"
              "(源码钉 + 阳性对照), 见 tests/test_ue_quote_freshness_shadow.py "
              "TestAliasConfinedToGateOffPaths。"
              "**到期触发**: 轮次自 2026-09-16 起算, 预计 **2026-09-29** 达 10 轮, 届时手工/会话跑 "
              "eval_script 呈裁; 判据未达前**不得**建议激活 (选项 (A)/(B) 按 2026-09-15 用户裁 (C) "
              "维持现状: 不透出 update_time、不动 SSOT、不动 90s 阈值)。"
              "断流无自动告警 (纯只读研究环, 与 tier-gate 影子同待遇), 靠台账 §6 与静默哨兵兜; "
              "**计数口径残留 (诚实披露)**: 周检 `refresh_counts` 用 `distinct:date` 数本文件行, "
              "而它不认 `status`/`forced_non_trading_day`, 故 `--force` 人工核查行有可能把接口计数"
              "抬 +1 ⇒ 到期判据**一律以裁决器为准** (裁决器按 status=ok ∧ 非 forced ∧ 交易日三重"
              "过滤); 接口计数只作攒样进度提示, 达标通知 (READY) 本身已写明 'READY≠毕业'。"
              "回滚 = 删 crontab 两行 + 删本条目 + 删两个文件 (零生产耦合、无调用方)。",
        eval_on_demand=True,
    ),
    # ── 2026-09-26 补登记批: 判据字典有、注册表查无的三条影子 (B19/B21/B22) ──
    # 立起理由 (实测危害, 非假想): 扫描判据字典「影子类」13 条 vs 注册表 58 条,
    # 三条有判据 + 有 cron + 有结算器却**注册表查无** ⇒ 周检 refresh_counts /
    # 挂载哨兵 / check_intent_uniqueness / pending_adjudication_report 全部看不到它们。
    # 实锤: B21 判据 n 轴实测 276 (门 20) 早已达标, 但因不在注册表 ⇒ **一个月从未被读过**
    # (本次补登记时实跑才得出「未过线」结论)。B22 是本 agent 上一轮自己登记的判据,
    # 同型欠账 — 说明缺口是结构性的 (登记判据字典时无人被强制同步注册表), 不是偶发。
    # 本批只补**可见性** (登记 + 计数锚同轴), 判据文/阈值逐字照抄不改 (改=重裁)。
    ShadowItem(
        id="eod_depth_gate",
        name="EOD 深水闸影子 (would_revoke record-only)",
        status=ShadowStatus.OBSERVING,
        description="B19 (RFC-079): EOD 豁免判定时刻净水深 ≤-2.5% 记 would_revoke "
                    "(record-only, 真仓照常豁免持夜); 次日结算 Δ=反事实EOD平−实际结局 "
                    "(费两边抵消=纯价差)。打点 app/playbook/overnight_decision."
                    "emit_eod_depth_gate_shadow, 结算 scripts/settle_eod_depth_gate_shadow.py "
                    "(crontab 30 6 * * 2-6, 台账 §2 `crontab-eod-depth-gate`)。"
                    "2026-09-22 RFC-095 身份锚定 v2 (判据零变更, 只修测量口径): 旧结算按"
                    "「最新已平仓」配仓 ⇒ MU 真仓 1503 被配成同日先平的 1501 且永久冻结 "
                    "(Δ −36.51 记成 −73.23); 另有未持夜行被计成隔夜样本、同仓多夜多行使 "
                    "n 轴(行数)与判据轴(仓数)不同轴。现改身份锚定 + 未持夜排除 + 按仓去重。",
        data_path="data/shadow/eod_depth_gate_report_latest.json",
        config_key="playbook.eod_depth_gate_pct",
        min_n=8,   # 单位 = would_revoke **已结算去重仓数** (n_trig), 不是 arm 行数
        count_mode="lines",  # .json 路径走 _count_samples 的 n 键分支 (本项 report 顶层 n=n_trig)
        graduation_criteria="B19: would_revoke 结算 n≥8 ∧ ΣΔ>0 ∧ 误杀赢家(abs(Δ)>费地板$5)≤1 "
                            "→ **提请转硬闸 (另立DEB+用户裁, 影子不自动生效)**; 不过线 → "
                            "维持豁免机制原状。窗内禁改: loser_overnight_max_depth_pct "
                            "(99=8/21 已裁决禁用, 永不复用) / EOD 豁免链行为; 阈值 -2.5% "
                            "预注册冻结 (改=重裁)。",
        eval_script="scripts/settle_eod_depth_gate_shadow.py",
        created="2026-09-08",
        last_evaluated="2026-10-14",
        verdict_history=[
            {"date": "2026-09-26", "verdict": "KEEP_SAMPLING (n=2/8)",
             "evidence": "补登记当日实跑: trigger_settled_n=2 (门 8, ✗) ΣΔ=+28.40 误杀=1 "
                         "(门 ≤1, ✓) ⇒ 判据 2/3, n 不足继续攒样。rows_total=29 (arm 行, "
                         "含同仓多夜), unsettled_backlog=6, not_held_n=1, "
                         "match_by={entry_price_qty:18, trade_ref:5}。"
                         "旁证 (不入判据): DB 侧 overnight_arm 深水下(≥2.5%) 5 笔 "
                         "ΣΔ=+422 (UBER 8/26 +121.55 / GAP 8/28 +173.49 / UBER 9/3 +105.70 "
                         "/ M 9/10 −110.50 / RIVN 9/21 +131.79), 与判据字典回溯证据 "
                         "(8/1-9/8 24笔 ΣΔ=+$410 误杀0) 同向, 但其中 3 笔在影子窗前 "
                         "(arm_date 最早 9/8) ⇒ 只能作背景, 不得当判据样本。"},
                         {"date": "2026-10-14", "verdict": "GATE_NOT_WORTH (终态, settler 机械判定)",
                          "evidence": "DEB-20261009 §42 (用户裁「登记」= 批准方案A): 10-14 06:30 "
                                     "settler 判负 — trigger_settled_n=9(≥8) / ΣΔ=−293.98(≤0) / "
                                     "miskill=5(>1); miskill 行 settled=true 永久冻结只增不减 ⇒ "
                                     "误杀≤1 数学不可达 ⇒ verdict 在任何未来样本下不变, "
                                     "转硬闸路径正式关闭 (「门非失灵, 是 −2.5% 深度区分度不足」; "
                                     "avoid 4 笔 +574.28 / miskill 5 笔 −868.26)。"
                                     "处置=record-only 续采, 停采须另裁。出处 "
                                     "docs/debates/DEB-20261009-event-to-trade-pipeline.md:2901-2940; "
                                     "判据字典 B19 已同日回写。10-15 06:30 报告复读: n=9 / "
                                     "ΣΔ=−293.98 / miskill=5 / verdict=GATE_NOT_WORTH 稳定。"},
        ],
        intent="overnight_loser_exempt",
        cross_ref=("overnight_kill_criterion", "eod_mfe_gate_shadow"),
        notes="🔗 与 overnight_kill_criterion (B15) 同意图族「水下仓该不该给隔夜豁免」, "
              "但**不同轴不互斥**: B15 管 w_week 臂整体修复率 (已 9/21 裁「维持」结案), "
              "本项只管其中**深水子集** (≤-2.5%) 是否该撤豁免。B15 聚合结论由浅水组撑起 "
              "(浅水下<1% 9笔差+380 / 中水下1-3% 16笔差+1289 / 深水下≥3% 5笔差**−422**, "
              "扛更优 1/5) ⇒ 本项是 B15 的残存子集, 不是替代。cross_ref 指 SUPERSEDED 项"
              "供裁决日对照, 意图守卫只对现役项强制互引故不触发告警。"
              "**计数锚同轴修复 (本批)**: report 原无 n/n_total/n_events/stats 任一键 ⇒ "
              "_count_samples 返回 None ⇒ current_n 恒 0 (gap_chase_gate 9/21 同型假象: "
              "被清账误读成「0 样本断供」)。现已在结算器补顶层 `n`=n_trig (判据轴=去重仓数, "
              "**不是** arm 行数 — 同仓多夜落多行, 用行数会虚假达标迁 READY)。"
              "回滚 = eod_depth_gate_shadow_enabled false + 重启 (热重载可读)。",
    ),
    ShadowItem(
        id="eod_mfe_gate_shadow",
        name="EOD「MFE<0.5% ∧ 水下」不过夜闸影子 (RFC-160, 2026-10-22 用户裁立项)",
        status=ShadowStatus.OBSERVING,
        description="RFC-157 预注册重议触发 (近 11 日 OGS 4 笔均 −$324.05 ≤ −$250) 后立项: "
                    "与 B19 深度轨同载体同结算器 (eod_depth_gate_shadow.jsonl + "
                    "settle_eod_depth_gate_shadow.py, cron 30 6 * * 2-6), 触发谓词换判别器: "
                    "arm 时刻「当日 MFE<0.5% ∧ 水下」 (10-17 尸检 §3: 灾难 4 笔 MFE 全 <0.5%, "
                    "恢复者 4/5 ≥0.6%; 机制=「当日从未盈利 0.5%」= 论点未被市场证实, "
                    "不配占用隔夜跳空敞口)。打点侧 arm 行新增 mfe_pct_at_arm "
                    "(overnight_decision.emit_eod_depth_gate_shadow 的 peak_price 入参, "
                    "两调用点 exit_engine/overnight_cleanup 已接)。零交易行为变更。",
        data_path="data/shadow/eod_depth_gate_report_latest.json",
        config_key="playbook.eod_depth_gate_shadow_enabled",
        min_n=8,   # 单位 = MFE 轨触发组去重仓数 (mfe_track.n), 与 B19 同构
        count_mode="json_key:mfe_track.n",
        graduation_criteria="RFC-160 预注册 (逐字, B19 形态沿用): 触发组去重结算 n≥8 ∧ "
                            "ΣΔ>0 ∧ 误杀赢家 (Δ<−$5 费地板) ≤1 → 提请转硬闸 (另立 DEB + 用户裁, "
                            "影子不自动生效); 不过线 → 维持豁免机制原状。窗内禁改: MFE 阈值 0.5% "
                            "(10-17 尸检刀尖注记: 0.4715 vs 0.4744 差 0.003pp, 精确切点不可信, "
                            "前瞻攒样的意义即消化切点风险; 改=重裁) / 水下定义 depth_net_pct<0 "
                            "/ EOD 豁免链行为。",
        eval_script="scripts/settle_eod_depth_gate_shadow.py",
        created="2026-10-22",
        notes="⚠️ 与 eod_depth_gate (B19) 的关系: 同载体不同判别器 — B19 深度轨 10-14 已终裁 "
              "GATE_NOT_WORTH (「−2.5% 深度区分度不足」), 该终裁**只关闭深度轨转硬闸路径**, "
              "不约束 MFE 轨 (判别器不同: 深度=价格幅度, MFE=论点证实度); 两轨在同一报告 "
              "并列读数 (顶层 verdict=深度轨, mfe_track.verdict=MFE 轨)。"
              "反证据基线并陈 (RFC-157 §6): EOD_FORCE_CLOSE 笔均 +$51.81 ⇒ 不是所有过夜都坏, "
              "一刀切平仓被明示排除, 本轨只测「MFE∧水下」窄人群。首批 arm 行的 mfe 字段自 "
              "2026-10-22 代码生效起有值; 存量行 mfe=None 不进分母 (不猜)。",
        intent="overnight_loser_exempt",
        cross_ref=("eod_depth_gate", "overnight_kill_criterion"),
    ),
    ShadowItem(
        id="eth_exit_forgone_upside",
        name="延长时段利润保护出场后「错失上行」影子 (B21)",
        # 2026-09-26 用户裁 A 案 (n 轴口径维持字面集合) ⇒ 判据三门 1/3 未过线,
        # 按 graduation_criteria 原文「不过线 → 维持现行『触线即出』语义, 本判据留档为反证」转 KILLED。
        # 注: refresh_counts 只对 OBSERVING/READY 迁移 (终态 continue) ⇒ 不会被拉回 READY。
        # 采集面: 载体是复用既有两条影子 + 结算器 cron (30 6 * * 2-6), 本项无独立写方
        # ⇒ 转 KILLED 不停任何采集, 结算器继续给读数 (留档为反证需要持续证据)。
        status=ShadowStatus.KILLED,
        description="B21: 量化「BE/锁盈触线即出」是否卖早了。载体复用既有两条影子 "
                    "(premarket_exit_shadow.jsonl / afterhours_exit_shadow.jsonl) + 既有"
                    "结算器 settle_extended_hours.py holdout (决策价→T+0收盘/T+1收盘…, "
                    "cron 30 6 * * 2-6 已挂, **零新代码**)。首样本 = MU 9/25 04:04 "
                    "SL_BREAKEVEN (决策价 1090.64)。背景: MU 案退出后盘前继续上行, 但富途"
                    "历史K线/快照均无盘前数据 (request_history_kline 9/25=0行), tick 级路径"
                    "唯一持久源 = 影子 jsonl ⇒ 错失上行只能由本环量化, 无法事后补拉。",
        data_path="data/extended_hours_settle_latest.json",
        config_key=None,
        min_n=20,
        count_mode="json_key:b21_exec_settled_n",
        graduation_criteria="B21 (预注册, 改=重裁): 执行类已结算 n≥20 ∧ d0_close_ret_pct "
                            "均值 > 费用线(~0.085%往返) ∧ ≥60% 为正 → **提案**「BE/锁盈出场后 "
                            "hold/trailing 宽限窗 (grace window)」另立 DEB+用户裁 (影子不自动"
                            "生效); 不过线 → 维持现行「触线即出」语义, 本判据留档为反证。"
                            "窗内禁改: breakeven_at_pct / 触发线公式 / 9-17 延长时段裁决"
                            "(只服务盘中持仓、只止盈不止损)。",
        eval_script="scripts/settle_extended_hours.py",
        created="2026-09-25",
        last_evaluated="2026-09-26",
        verdict_history=[
            {"date": "2026-09-26", "verdict": "NOT_MET (判据 1/3, 但 n 轴口径需重裁)",
             "evidence": "补登记当日实跑三门: ①n≥20 ⇒ **n=276 ✓**; ②d0_close 均值>0.085% ⇒ "
                         "实测 **−0.2249% ✗** (ledger 加权 −0.2364%); ③≥60%为正 ⇒ 实测 "
                         "**18.2% ✗** (正52/零94/负139, n=285) ⇒ 判据未过线, 维持「触线即出」。"
                         "⚠️ **n 轴虚达标 (诚实披露, 不得当达标证据)**: 276 笔里 "
                         "**tighten 占 277/285 = 97.5%** — tighten 是「收紧止损参数」不是实际"
                         "出场, 判据文把它列进执行类集合属登记时未分辨语义。剔 tighten 后"
                         "**真出场子集 n=8** (lock_in 3 / breakeven_lock 2 / SL_TRAIL_LOCK 2 / "
                         "SL_BREAKEVEN 1), 均值 −13.12% 正值率 12.5% — 同样未过线但 **n<20 "
                         "不足裁**。两口径同向 (都指向不提案) 故本次结论稳健; 但②③门若按真出场"
                         "口径重算需 n≥20 ⇒ **判据集合是否剔 tighten 须用户裁 (改判据=重裁, "
                         "本 agent 不擅自改)**。另 94 个 t0_close_ret_pct=0 行全是 tighten "
                         "(决策价=收盘价, 疑似无实际出场时点可结算), 亦属同一口径问题。"},
        ],
        intent="eth_exit_semantics",
        cross_ref=("eth_exit_quote_age",),
        notes="🔗 与 eth_exit_quote_age (B22) 同载体同结算器同意图族「延长时段出场语义」, "
              "互指防双转正打架: 本项若过线提案 grace window (放宽触线即出), B22 若过线提案"
              "报价龄门 (超龄不执行/降级 record-only) — 两者都改同一条出场链, 同时过线须先裁"
              "归属, 不得各自独立生效。**计数锚同轴 (本批新增 json_key:)**: 源 jsonl 5739 行里 "
              "89% 是 hold_no_stop (亏损侧按 9-17 裁决本不执行) ⇒ count_mode=lines 会让 "
              "current_n 立刻虚假达标迁 READY; 改由结算器算 `b21_exec_settled_n` (执行类 ∧ "
              "已结算) 当轴, 注册表用 json_key: 取。**只印不判**: 结算器只给计数与分桶读数, "
              "不写 verdict (防「报告写死方向性结论随数据翻转把裁决带反」9/23 实锤)。",
    ),
    ShadowItem(
        id="eth_exit_quote_age",
        name="延长时段退出「报价龄」测量判据 (B22)",
        status=ShadowStatus.OBSERVING,
        description="B22: 测延长时段出场时的报价陈旧度是否造成成交劣化 (决策价 vs 实际成交价"
                    "偏离 / exit_slippage_bps)。样本 = 两条 ETH 退出影子 jsonl 中**执行类** "
                    "action 行 ∧ quote_age_seconds 非空 (该字段 9/18 起采集, 语义=futu 快照 "
                    "update_time 距采集时刻的秒龄, 交易所侧时戳口径, 同 B20)。结算复用既有 "
                    "settle_extended_hours.py, **零新代码**。",
        data_path="data/extended_hours_settle_latest.json",
        config_key=None,
        min_n=20,
        count_mode="json_key:b22_exec_aged_n",
        graduation_criteria="B22 (预注册, 改=重裁): 执行类带龄行 n≥20 时读数 — ①龄>120s 的"
                            "执行行数 ≥1 **且** 其成交劣化较龄≤30s 组中位差 >20bps → **提案**"
                            "报价龄门 (超龄不执行/降级为 record-only; 另立 DEB+用户裁, 影子不"
                            "自动生效); ②龄>120s 执行 = 0 或两组无差异 → 维持现状 (不加龄门), "
                            "本判据留档为反证。窗内禁改: quote_age_seconds 采集语义 / 9-17 "
                            "延长时段裁决。",
        eval_script="scripts/settle_extended_hours.py",
        created="2026-09-25",
        last_evaluated="2026-09-26",
        verdict_history=[
            {"date": "2026-09-26", "verdict": "KEEP_SAMPLING (n=6/20, 分支②方向)",
             "evidence": "补登记当日实跑: 执行类带龄行 n=6 (门 20, ✗) 分桶 {>120s: 0, "
                         "<=30s: 6} ⇒ **龄>120s 执行 = 0**, 落判据分支② (维持现状不加龄门) "
                         "方向, 但 n<20 不足裁, 继续攒样。既有读数 (9/16-9/25, 判据字典留档): "
                         "MU 2.2s / WDC 2.9s / ACN 28.9s / WBD 21.9s 全部≤30s; 龄>120s 的 586 "
                         "行 100%=hold_no_stop (亏损侧按裁决本不执行) ⇒ 超龄只发生在"
                         "**不执行**的行上, 与「超龄致成交劣化」假设天然无交集。"
                         "龄>120s 集中于低流动票 (MPC 54.8%/RIVN 34.4% 行), MU/AVGO/ORCL=0% "
                         "⇒ 报价密度是**标的属性非时段属性** (9/25 活体探针: MU/AVGO/ORCL "
                         "快照龄<4s, MPC 12min/AEHL 21min; LV3 TICKER 盘前推昨收不含 ETH 成交, "
                         "快照 pre_price 即盘前权威源)。"},
        ],
        intent="eth_exit_semantics",
        cross_ref=("eth_exit_forgone_upside",),
        notes="🔗 与 eth_exit_forgone_upside (B21) 同载体同结算器同意图族, 互指见该条 notes。"
              "**本项 n 轴与 B21 刻意不同** (B21=执行类∧已结算 / B22=执行类∧带龄, 且 B22 判据"
              "文的 action 集合另含 pre_close_flatten 比 B21 多一值) ⇒ 同一 report 需两个键, "
              "这是本批给 _count_samples 加 `json_key:` 的直接原因 (固定四键只能取一个, 无法"
              "同轴服务两条判据)。集合差异逐字照判据文, 不做「顺手统一」(改集合=改判据=重裁)。"
              "**攒样预期诚实披露**: 执行类带龄行 9/16-9/26 十天仅 6 行 ⇒ 按此速率到 n=20 需"
              "约一个月, 且分支①要求「>120s 的执行行 ≥1」— 而 >120s 迄今 100% 落在不执行的 "
              "hold_no_stop 行上 ⇒ 本项**可能长期停在分支②**(留档反证)而非攒到可裁, 这是"
              "判据设计本身的预期结局之一 (判据文已写明②=维持现状留档为反证), 不是断供。",
    ),
    ShadowItem(
        id="fee_gate_shadow",
        name="T3 费用 C 影子门槛",
        status=ShadowStatus.OBSERVING,
        description="费用路径 C 的前向反事实影子: 对 2026-09-20 起入场且已平仓的 "
                    "PLAYBOOK 交易 (trade_date=入场日, 入场门反事实按入场日选样), "
                    "逐笔记录 entry_price/quantity/notional/fees_est/fee_bps/"
                    "pnl_net 与价格门、名义额门、组合门的 would_block。只读采集, "
                    "不改入场、仓位、订单、配置或退出参数。",
        data_path="data/fee_gate_shadow.jsonl",
        config_key=None,
        min_n=30,
        count_mode="distinct:trade_id",
        graduation_criteria="n≥30 (distinct trade_id) 后机械产出各候选门 blocked/kept "
                            "净期望、胜率、费用 bps 与 n-loss; 10/11 结合影子读数与"
                            "双窗口回放终裁真门槛。样本不足或读数反向时禁止提议转正。",
        eval_script="scripts/fee_gate_shadow.py",
        created="2026-09-20",
        current_n=0,
        notes="cron `45 6 * * 2-6`; 前向汇总落 data/fee_gate_shadow_latest.json。"
              "$9,950 已被 §十六证伪, 不作为候选; 本影子只并记价格<$1、名义<$4k、"
              "<$5k 与价格≥$1+名义≥$4k 组合。回滚=删 cron 行+注册条目+两个数据产物。",
        eval_on_demand=False,
        intent="fee_floor_cost_guard",
        cross_ref=(),
    ),
    # ── 延长时段 (ETH) 链路台账 —— 2026-09-23 注册 (DEB-20260923 §11 N3) ──────────
    # 背景: 本链 6 个台账此前**一个都没注册**, 毕业判据/样本计数/停摆守卫全靠各脚本手搓,
    #   于是"记录器停摆无人知"这类历史坑只能靠人工巡检发现。注册后由本注册表的
    #   check_mounting()/refresh_counts() 统一承担 (框架能力, 不再重复造)。
    #   注: 同批的 L2 状态缓存 (RFC-101) 已于同日回滚 ⇒ 不注册该项。
    ShadowItem(
        id="eth_rth_fade",
        name="RTH fade 事件空头影子",
        status=ShadowStatus.OBSERVING,
        description="盘中事件驱动做空 (rth_fade): 对当日有同相位财报事件的票记 record-only 读数 + 平行止损臂。零下单。",
        data_path="data/rth_fade_shadow.jsonl",
        min_n=30,
        graduation_criteria="≥15 个交易日且 ≥30 笔; 净/笔>0; 胜率≥55%; 留一口≥0 —— 现 1 日/3 笔, 净/笔>0 ✅ 胜率 33% ❌",
        eval_script="scripts/rth_fade_graduation_check.py",
        created="2026-09-23",
        notes="幂等: 按 (symbol, rth_day) 去重 (写入侧+读取侧双防, 防同日重跑翻倍污染)。",
    ),
    ShadowItem(
        id="eth_earnings_direction_path",
        name="财报相位方向路径影子",
        status=ShadowStatus.OBSERVING,
        description="BMO/AMC 相位 × 跳空方向分档的路径读数 (RFC-099 判据来源), 逐日一行。",
        data_path="data/earnings_direction_path_shadow.jsonl",
        min_n=30,
        graduation_criteria="相位标定覆盖率稳定 + 分档方向一致性成立 ⇒ 提案转正; 与 RFC-099 §5 判据逐条对账。"
                            "攒样器头部预注册读数门槛 (逐字): **每方向 ≥30 笔 且 每方向净/笔 > 0**; "
                            "分档按 RFC-099 §4: gap≥+3.0%⇒SHORT / +0.5%≤gap≤+2.0%⇒LONG / 其它⇒NONE。"
                            "⚠️ 注意 min_n=30 数的是 jsonl **总行数**, 与「每方向 ≥30 笔」是两个轴 (见 notes)。",
        eval_script="scripts/earnings_direction_path_eval.py",
        eval_on_demand=True,
        created="2026-09-23",
        notes="零下单; 相位来源 futu 日历 ∪ Finnhub (并源), 空相位实测 11.8%。"
              "🔧**eval_script 换体 (2026-09-26 RFC-109④-D)**: 原指 scripts/earnings_direction_check.py "
              "= RFC-099 的**一次性判据实证**脚本 (9/23 为回答「财报日如何选多空」而写), 三个结构性问题: "
              "①它读 /root/.hermes/cache/scratch/replay_earnings_criterion_final.json (118 条回放产物), "
              "而 scratch **72h 自动清理** ⇒ 实测 FileNotFoundError ⇒ eval 结构性不可重跑 (尺子坏了); "
              "②该脚本**无 main() 保护** (顶层直接执行) ⇒ import 即跑, 不是可复用评估器; "
              "③它读的 118 条回放与本影子入库产物**不是同一数据源** ⇒ 就算能跑也不是在评这条影子。"
              "新器 scripts/earnings_direction_path_eval.py 改读入库产物 (cron 20 4 * * 2-6 攒样), "
              "可无限重跑; 只读零副作用 (不推送/不写库/不写盘/不下单); eval_on_demand=True "
              "(按需评估不挂 cron, 同 sltp_verify_shadow / iv_hv / short_funnel 等 11 条先例) "
              "⇒ 挂载哨兵的「悬空观察」告警解除 (该告警报的正是原 eval 不在 cron 面)。"
              "📐**实测读数 (9/26, 判据未过 ⇒ 维持攒样)**: n=32 行 / 4 个 distinct 日 (9/22~9/25)。"
              "①相位标定正确率 **32/32=100%** (gap≥+3% 10/10 SHORT, +0.5~2% 2/2 LONG, 其它 20/20 NONE) "
              "但 distinct 日仅 4 天 ⇒ 「覆盖率**稳定**」判不了 (只是单批一致)。"
              "②分档方向一致性: SHORT n=10 净中位+0.80% 净均值+3.17% 胜率 50% (净>0 ✓ 但 n<30 ✗); "
              "LONG n=2 净中位−1.30% (n<30 ✗ 且净<0 ✗)。⇒ **两方向都未达门槛, 不提案转正, 生产参数不动**。"
              "⚠️**计数轴错配 (如实披露, 未擅改)**: min_n=30 数总行数 (现 32 ⇒ refresh_counts 机械提升 "
              "READY_TO_EVALUATE), 而预注册门槛是**每方向 ≥30 笔** (SHORT 10 / LONG 2) ⇒ "
              "**总行数达标 ≠ 判据达标**。改 min_n = 改判据门槛 = 重裁, 须用户裁, 本批不动。"
              "按当前速率 (4 天攒 32 行, 其中应标定 12 行) LONG 档攒到 30 笔需较长时间 ⇒ 已挂 recheck 窗。",
    ),
    ShadowItem(
        id="eth_ext_entry_prefilter",
        name="入场判据①②对照影子",
        status=ShadowStatus.SUPERSEDED,
        description="现行判据 vs 对照判据在**同一批候选**上的裁决对照 (旧判据在生产误拦了多少)。只记账。",
        data_path="data/ext_entry_prefilter_shadow.jsonl",
        min_n=30,
        graduation_criteria="对照显示旧判据误拦持续存在且方向一致 ⇒ 提案切换判据; 影子与生产裁决不一致率需可解释",
        eval_script="scripts/eth_stage_daily_report.py",
        created="2026-09-23",
        last_evaluated="2026-10-03",
        collection_retired=True,
        cross_ref=("ext_entry_exec_price_shadow",),
        verdict_history=[{"date": "2026-10-03", "verdict": "SUPERSEDED (同轴由 $50 执行池影子接管)",
                          "evidence": "仅 5/30 行且 2026-09-18 后断流; 现行 cron 不传 "
                                      "EXT_PREFILTER_SHADOW, 默认 false。DEB-20260928 "
                                      "同轴枚举已将其列为旧案, 新影子 546 轮覆盖同人群 "
                                      "price_band/$50 二分反事实。"}],
        notes="观测与决策解耦: 判据评估已前移到涨幅带/量闸之前, 否则样本被下游闸截断。",
    ),
    ShadowItem(
        id="ext_entry_exec_price_shadow",
        name="EXT 执行池价格 $50 影子 (record-only) (⛔已退役 2026-10-19)",
        status=ShadowStatus.KILLED,
        description="EXT 候选池运行成本/干扰观测 (非绩效影子): $5→$50 执行池价格下限的"
                    "逐轮反事实计数 — 复用已取榜单/快照价记 candidates_n / "
                    "current_price_pool_n / shadow_execution_pool_n + prefiltered_out 明细。"
                    "零新增富途请求, 零交易行为变更 (记录器在全部生产闸之后, "
                    "EXT_ENTRY_MIN_PRICE 仍 $5)。立案 = DEB-20260928-ext-exec-price50-shadow。",
        data_path="data/ext_entry_exec_price_shadow.jsonl",
        config_key="EXT_ENTRY_EXEC_PRICE_SHADOW",
        min_n=60,
        graduation_criteria="DEB §三 预注册 (逐字): 观测窗 2026-09-28 18:05 CST 起, 至少 "
                            "3 个交易日 / ≥60 行; 必须报告: ①candidates_n/current_price_pool_n/"
                            "shadow_execution_pool_n 的中位数与分布 ②每轮 $5→$50 的候选池缩减比例 "
                            "③实际 EXT 成交中 entry_price<$50 的占比 (被排除人群) ④影子价池与"
                            "实际下单/成交的交集。禁止: 本影子自动改 EXT_ENTRY_MIN_PRICE; "
                            "n<30 成交样本裁绩效; 把「影子价池更小」单独解释为「富途请求减少」。"
                            "切换生产须另立 DEB + 用户裁。死线 2026-11-30 自动停采 "
                            "(防僵尸影子, eth_relative_volume_gate 同型先例)。",
        eval_script="scripts/ext_entry_exec_price_eval.py",
        eval_on_demand=True,
        created="2026-09-28",
        last_evaluated="2026-10-19",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-19", "verdict": "KILLED_REASONABLENESS",
                          "evidence": "1686 轮/15 天; 实际 EXT 成交 5 笔全 >$50, 低价成交 0, "
                                      "交集 0; $50 门槛中位缩减 86.4% 价格池; 观测问题已回答, "
                                      "继续采集无新增信息; crontab 两行内联开关 false; 数据保留; "
                                      "重开=新预注册."}],
        cross_ref=("eth_ext_entry_prefilter",),
        notes="P0.5 同轴枚举 (2026-09-28 复核后补登, 立案时漏查注册表违反当日 template 新增"
              "强制项): ①eth_ext_entry_prefilter — 同人群(盘前入场候选)同轴(price_band 五档含 "
              "$50~200)且粒度更细, 但末行 2026-09-18、EXT_PREFILTER_SHADOW 默认 false 且 "
              "crontab 未传 ⇒ 已停采 (n=5/30 幽灵在册, 其处置属另案裁决面); 本案非其复活 — "
              "本案按轮聚合记 $5→$50 二分反事实, 开关走 crontab 行内联真在采。"
              "②ticket_size_breakeven — 费用地板随价格递减的机制推导已定案 ($5 档 52.6bps "
              "全表最贵), 本案不在费用/绩效轴重复攒样 (DEB §三 禁止2)。"
              "③DEB-20260928-supply-price-tier-shadow 已撤回, 本案不复活供给配额。"
              "count_mode=lines: 一行=一轮 cron 运行聚合, 天然按轮去重。"
              "2026-10-19 只读读数 (scripts/ext_entry_exec_price_eval.py): 1686 轮/15 天; "
              "current_price_pool 中位 14, shadow_execution_pool 中位 2, 中位缩减 86.4%; "
              "prefiltered 19,352 条/995 symbols; 实际 EXT 成交 5 笔全 >$50, 低价成交 0, 交集 0; "
              "结论=数据已可读数, 维持/退役待裁。"
              "2026-10-19 用户令「按合理性和建议执行」⇒ 退役: 1686 轮/15 天, "
              "$50 门槛中位缩减 86.4%, 实际 EXT 成交 5 笔全 >$50/低价 0/交集 0; "
              "crontab 两行内联 EXT_ENTRY_EXEC_PRICE_SHADOW 改 false 真停采; "
              "数据保留; 重开=新预注册。",
    ),
    ShadowItem(
        id="eth_short_probe",
        name="盘前做空通道探针 (⛔已退役 2026-10-18)",
        status=ShadowStatus.KILLED,
        description="盘前做空通道可达性实证 (能下单/能成交/能建空仓/能平回), 1 股 SIM, 每日 ≤3 次。",
        data_path="data/eth_short_probe.jsonl",
        config_key="EXT_ENTRY_ALLOW_SHORT",
        min_n=3,
        graduation_criteria="见 RFC-099 §10 四条回滚触发 (§10.1 更正: 原文实为 4 条编号项, 触发4观测面以②闸计数为唯一可执行口径); 复核日 2026-10-07 (逐日 attempt 上限=额度, 非样本量目标)",
        eval_script="scripts/probe_eth_short_open.py",
        created="2026-09-23",
        verdict_history=[{"date": "2026-10-18", "verdict": "KILLED",
                          "evidence": "RFC-099 §10 探针已完成可达性实证 (4 行, 9/23-9/26); "
                                      "crontab 一次性调度 2026-10-06 已删, 产物 9/26 后冻结; "
                                      "复核日 2026-10-07 已过, 无生产消费方 ⇒ 减法批退役. "
                                      "数据保留不删; 重开=新预注册, 不许只翻开关."}],
        collection_retired=True,
        notes="选标按价差最窄且 ≤1%, 无合格返回 no_liquid_candidate (不下一张注定不成交的单)。"
              "2026-10-18 减法批: 退役 (cron 已删/数据保留); 重开须新预注册。",
    ),
    ShadowItem(
        id="eth_premarket_exit_shadow",
        name="盘前退出影子",
        status=ShadowStatus.OBSERVING,
        description="盘前持仓的逐分钟退出判据读数 (利润保护/亏损侧只记不动), 逐分钟一行。",
        data_path="data/premarket_exit_shadow.jsonl",
        min_n=30,
        graduation_criteria="水下保本族/时间退出族净额>0 且样本 ≥30 ⇒ 提案; 与 EXT_CERT_LOCK_PCT 现行裁决对账",
        eval_script="scripts/settle_extended_hours.py",
        created="2026-09-23",
        notes="现行裁决: 延长时段只做利润保护, 亏损侧不执行止损 (underwater_no_extended_loss_stop)。",
    ),
    ShadowItem(
        id="eth_afterhours_exit_shadow",
        name="盘后退出影子",
        status=ShadowStatus.OBSERVING,
        description="盘后持仓的逐分钟退出判据读数 (与盘前同族), 逐分钟一行。",
        data_path="data/afterhours_exit_shadow.jsonl",
        min_n=30,
        graduation_criteria="同盘前退出影子; 两时段合并评估需先证分布无系统性差异",
        eval_script="scripts/settle_extended_hours.py",
        created="2026-09-23",
        notes="盘后窗 16:00–19:59 ET; 收口强平 EXT_PREMARKET_FLATTEN_FROM=925 只作用于当日进场仓。",
    ),

    # ── RFC-104 阶段 0: ETH 事件执行计划 (只读工件, 2026-09-24 用户裁立项) ──
    ShadowItem(
        id="rfc104_eth_event_plan",
        name="RFC-104 ETH 事件执行计划 (阶段 0 只读影子) (⛔已退役 2026-10-15)",
        status=ShadowStatus.KILLED,
        description="RFC-104 阶段 0: 从 futu+Finnhub 日历源生成 T 日事件计划 "
                    "(data/loops/ext_entry_plan/<et_date>_<session>.json), 每票一行含状态机字段/"
                    "源标注/ambiguous 处理。零交易行为变更; 计划**不含**方向/SL/TP/费用/"
                    "可执行性数字 (三处收紧 §3); 不预筛 price/volume/mcap (收紧 §1); "
                    "源冲突不投票不择优 → state=ambiguous (收紧 §2)。"
                    "消费侧接入是阶段 1+ (本阶段不修改 extended_hours_entry_executor.py)。",
        data_path="data/loops/ext_entry_plan/graduation_check_latest.json",
        config_key=None,
        min_n=7,  # 7 个交易日覆盖 = 阶段 0 通过判据 (当日事件票 ⊆ 计划 = 100%)
        count_mode="json_key:criteria.trading_days.actual",
        graduation_criteria="RFC-104 §3 阶段 0 通过判据: "
                            "①当日事件票 ⊆ 计划 = 100% (覆盖率) "
                            "②计划条数带 [0,44] (2026-09-24 用户裁修订: 原 7-9 系单源起草估算, "
                            "多源并集实测 16 行超带; 修订先于 7 日验收窗结束, 下界 0=无财报日合法, "
                            "上界 44=盲扫基线防计划爆炸) "
                            "③连续 7 个交易日无漏票 ⇒ 阶段 1 (影子消费) 立项; "
                            "回滚: 删 cron 行 (计划只读工件, 删除即静默)。",
        eval_script="scripts/eth_event_plan_graduation_check.py",
        created="2026-09-24",
        last_evaluated="2026-10-15",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-15", "verdict": "NOT_READY (阶段0 判据未过, 阶段1 不立项)",
                          "evidence": "graduation_check 10-15 10:08 自动跑 (cron 8 10 * * 2-6): "
                                     "ledger 33 行/15 交易日 ⇒ ①交易日数 15≥7 ✅ ②覆盖率100% 仅 "
                                     "6/15 日 ❌ (9 日漏票) ③条数带 [0,44] ❌ (09-28 两窗 45/49, "
                                     "均值 47.0)。两个待裁 (ADJUDICATED 同载): 阶段0 是否实质 FAIL; "
                                     "上界 44 持续被破属判据问题还是计划层退化 (改判据=重裁)。"},
                         {"date": "2026-10-15", "verdict": "PHASE0_FAIL (终态, 用户裁「判阶段0 FAIL 关停」)",
                          "evidence": "覆盖率判据 15 交易日从未全 PASS (最好 6/15, 漏票 9 日: "
                                     "09-24/28/30, 10-01/06/07/08/09/13) + 条数带上界 44 持续被破 "
                                     "(均值 47.0) ⇒ 阶段0 未过, 阶段1 永不立项。处置: 全 8 行 cron "
                                     "注释退役 (生成×2/刷新×3/对账×2/毕业检查×1, 备份 "
                                     "/tmp/crontab_backup_20261015_rfc104.txt), 台账行加退役标, "
                                     "计划目录与 reconcile_ledger 留档不删; 复活须重新预注册 "
                                     "(改判据=重裁)。"}],
        notes="调度: 盘前 T-1 20:30 ET (CST 08:30 Tue-Sat) + 盘后 T-0 15:45 ET (CST 03:45 Tue-Sat); "
              "幂等键=(et_date, symbol, session, phase); 原子写 app/utils/atomic_write.py; "
              "三处收紧 (REVIEW-20260924 §3): ①不预筛 ②不择优 ③不是策略卡; "
              "状态机: planned→armed→waiting_data→ordered/rejected/expired; "
              "回滚=删 cron 两行 + 计划目录 (计划只读工件, 删除即静默); "
              "注册表 hash 漂移: 本条目新增会使 review bundle hash 变化 (已知 N-9 现象)。",
    ),

    # ── ETH 微观结构三影子 (提案 2026-09-24 用户裁「批」, 判据先于读数写死于
    #    docs/analysis/2026-09-24-eth-microstructure-shadow-preregistrations.md) ──
    ShadowItem(
        id="eth_relative_volume_gate",
        name="事件相对量闸影子 (提案⑤)",
        status=ShadowStatus.OBSERVING,
        description="对计划内事件票被**绝对量闸**拒绝的票-轮, 记录相对自身近 20 交易日"
                    "同时段周转中位的比值 (阈值 1.5 写死, 1.2/2.0 仅敏感性) + would_pass_relative"
                    " + cf_net_usd 反事实 (taker 语义含全费, 上界披露; 不可靠构造则 null+cf_basis 不硬算)。"
                    "对冲量闸墙 (RFC-104 §1 出单第一卡点); 与 RFC-103 绝对放宽影子互为对照臂。"
                    "自门控: RFC-104 阶段0 reconcile PASS <7 日时 dormant。record-only 零下单。",
        data_path="data/shadow/eth_relative_volume_gate_shadow.jsonl",
        config_key=None,
        # 📐 count_mode 口径修复 (2026-10-08 RFC-139 复查修复批 P1, 判据/阈值逐字未动):
        # 旧值缺省 lines 把**全部拒单采样行**计入 current_n — 实测 53 行全部
        # turnover_too_low ∧ would_pass_relative=None, 而判据 n 轴 = would_pass 样本 n≥30
        # ⇒ 53/30 虚假迁移 READY_TO_EVALUATE, 把「真轴 0/30 ∧ 交易日 1/15」抬进待裁面
        # (违反「count_mode 必须与判据 n 轴同轴」军规, short_chase_ban_shadow 9/18 同型)。
        # ≥15 交易日轴 count_mode 单轴表达不了, 结算时由判据脚本双轴同查。
        count_mode="where:would_pass_relative=True",
        min_n=30,
        graduation_criteria="提案§⑤预注册 (读数前写死): ≥15 交易日 ∧ would_pass_relative 样本 n≥30 ⇒ "
                            "①cf_net_usd/笔中位数>0 ②胜率≥55% ③剔除最强单日仍>0 → 立 DEB 提请相对量通道; "
                            "任一不满足 → 弃案停采留档。死线 2026-11-30 自动停采 (防僵尸影子)。",
        eval_script="scripts/eth_relative_volume_gate_shadow.py",
        created="2026-09-24",
        stale_expected=True,  # 自门控 dormant (reconcile PASS<7 日不写产物) ⇒ 超龄是预期态,
                              # 非断供 (2026-10-22 全链路复查 R7 假阳性收口)
        notes="median basis=前向自积累 (延长时段量历史不可得 — carry 影子先例+本次探查双确认; "
              "禁 RTH 全天量冒充盘前量); turnover_approx=decision_price×ask_volume 为近似口径如实披露 "
              "(gate 真实 volume 未落拒绝台账), ratio 判据自参照故口径一致即可比; 反事实无排队/冲击建模=上界。"
              "💤**n=0/产物缺失 = 刻意 dormant, 非断供非「触发不存在」(2026-09-26 RFC-109④-B 核实)**: "
              "本影子自门控 — 写方脚本 scripts/eth_relative_volume_gate_shadow.py:320 `_check_reconcile_gate()` "
              "要求 data/loops/ext_entry_plan/reconcile_ledger.jsonl 的 PASS 日数 ≥7 才开跑, 未满则打印 "
              "`[eth_vol_gate] dormant: reconcile PASS 日数 N/7` 后直接退出 (不写产物)。实测: cron 在跑 "
              "(0 22 * * 1-5 premarket / 20 9 * * 2-6 afterhours), 日志 9/26 09:20 = `dormant: 2/7` ⇒ "
              "门控正常推进 (reconcile_ledger 7 行: PASS 2 / FAIL 2 / NO_DATA 3), 只是还没到 7 日。"
              "⇒ **不得据此裁退役** (原审计 B 项把「产物文件不存在」归为「触发不存在, 可裁退役」对此条不成立: "
              "created=2026-09-24 仅 2 天, 判据自带死线 2026-11-30 自动停采, 提前退役会砍掉正在等门控的影子)。"
              "⚠️**关联真缺陷 (非本条, 属 rfc104_eth_event_plan 计数锚)**: 门控的实时源 reconcile_ledger.jsonl "
              "每日 09:04 更新 (现 2/7), 但注册表 rfc104 的计数锚 data_path 读的是 "
              "graduation_check_latest.json — 该产物**停在 9/24 17:32 且写方 eth_event_plan_graduation_check.py "
              "无 cron 挂载** ⇒ 计数锚 n 永远停在 1/7 (陈旧快照), 与实时 2/7 脱节。此缺陷在并行 agent "
              "未提交的 rfc104 hunk 范围内, 本批不碰, 已上报用户待裁。",
    ),
    ShadowItem(
        id="eth_patient_maker",
        name="耐心挂单方入场影子 (提案⑥)",
        status=ShadowStatus.OBSERVING,
        description="对计划内、除价差/深度外全闸通过的票-轮, 纸面模拟 maker 入场: 买入挂 bid+1tick/"
                    "卖出挂 ask−1tick, 撤单=窗尾或不利漂移>0.5% (写死), 成交判定=后续可观测价穿越挂单价"
                    "(乐观上界, 无排队建模, 行内披露); 记 fill_rate/成交后窗内逆行/spread_captured/net_econ。"
                    "价差从成本变利润 — 对冲盘前价差墙 (CBRL 12.4% 实测 taker 数学不可做)。"
                    "颗粒度=既有台账原生 5min (granularity 字段如实记), 零新增 futu 轮询。"
                    "自门控同⑤ (reconcile PASS ≥7 日)。record-only 零下单。",
        data_path="data/shadow/eth_patient_maker_shadow.jsonl",
        config_key=None,
        min_n=20,
        graduation_criteria="提案§⑥预注册 (读数前写死): 模拟入场 n≥20 ⇒ ①fill_rate≥40% "
                            "②成交后窗内逆行中位数<捕获价差 ③净经济/笔中位数>0 → 提请 1 股 SIM 实弹探针"
                            " (沿 DEB-20260923 B 模式另行触发; 探针通过≠通道获批, §7.3 ≥40笔/≥15日回放门同款); "
                            "任一不满足 → 弃 maker 通道停采留档。死线 2026-11-30。",
        eval_script="scripts/eth_patient_maker_shadow.py",
        created="2026-09-24",
        notes="数据面=pretrade_gate_rejects 的 spread_too_wide/insufficient_depth 行 quote{bid,ask,spread_pct}"
              " + executions 台账; 穿越即成交是乐观上界 (排队位置未建模) — 判据过上界仍须实弹复核。"
              "💤**n=0/产物缺失 = 刻意 dormant, 非断供非「触发不存在」(2026-09-26 RFC-109④-B 核实)**: "
              "与 eth_relative_volume_gate 同一自门控 — scripts/eth_patient_maker_shadow.py:80 "
              "`RECONCILE_PASS_MIN_DAYS=7` + :371 `_check_reconcile_gate()` 要求 "
              "reconcile_ledger.jsonl PASS 日数 ≥7 才开跑, 未满则打 `[eth_maker] dormant: N/7` 后退出 (不写产物)。"
              "实测: cron 在跑 (5 22 * * 1-5 premarket / 25 9 * * 2-6 afterhours), 日志 9/26 09:25 = `dormant: 2/7`。"
              "⇒ **不得据此裁退役** (原审计 B 项「产物不存在=触发不存在, 可裁退役」对此条不成立: "
              "created=2026-09-24 仅 2 天, 判据死线 2026-11-30 自动停采)。门控推进后两影子会同时开写。"
              "关联真缺陷 (rfc104 计数锚读陈旧快照) 见 eth_relative_volume_gate notes, 不重复。",
    ),
    ShadowItem(
        id="overnight_known_event_flag",
        name="隔夜已知事件标记影子 (提案⑦)",
        status=ShadowStatus.OBSERVING,
        description="每日 CST 08:45 (--next-day) join open_positions (owner_strategy IN playbook_sim/"
                    "extended_hours_entry, sqlite ro) × 次日盘前计划 (RFC-104 工件): 命中→flag "
                    "overnight_into_known_event, 未命中→对照行 overnight_no_known_event, 计划断供→plan_missing "
                    "如实留痕。零干预 (不写 DB/不推送/不触退出链) — 纯标记攒样 (RIVN 9/23 隔夜跳空 −$319 教训: "
                    "计划层提前知道事件日历, 持仓穿越已知事件此前是不可见风险状态)。无自门控 (只依赖计划存在性)。",
        data_path="data/shadow/overnight_known_event_flags.jsonl",
        config_key=None,
        min_n=20,
        graduation_criteria="提案§⑦预注册 (读数前写死): ≥20 个隔夜持仓观察 (flagged≥8) ⇒ "
                            "flagged cohort 单笔 p10 < −2% (尾部显著劣于 unflagged 同分位) → 提请"
                            "「事件穿越隔夜仓风险处置」DEB (收紧/减仓/显式 carry 理由, 届时另裁不预设结论); "
                            "否则维持现状留档结题。死线 2026-12-31 (隔夜样本低频, 死线放宽一档)。",
        eval_script="scripts/overnight_known_event_flag.py",
        created="2026-09-24",
        notes="运行时刻语义: cron CST 08:45 = ET 20:45 (T-1) ⇒ --next-day 目标 ET 日 T 的盘前窗 "
              "(计划 CST 08:30 刚生成, 距盘前开 CST 16:00 约 7h); plan_scope=premarket (盘后计划此时"
              "合法不存在, 不算 missing); 幂等键 (et_date, symbol); PnL 读数器 --report 净额铁律 "
              "(pnl_net 优先否则 realized_pnl−fees_est)。",
    ),

    # ── 2026-09-26 RFC-109② 看板段补登批 (用户裁「按合理建议顺序执行」) ──────────
    # 根因: shadow_board 的产物绕过注册表硬编码 ⇒ 无 status/判据/停采开关, 既不能裁决
    # 也不能退役 (永久采集)。本批把看板 10 个未登记产物 (审计点名的 9 段 + 段4
    # edge_gate_shadow, 2026-09-26 实测同病) 纳入同一生命周期。
    # 补登纪律: 已有预注册判据的**逐字抄录**出处; 无判据的如实挂 NO_CRITERIA /
    # PERMANENT_MEASURE (补登时**不得虚构数值门** = 事后判据); 已退役的登终态留痕
    # (markout_capture 2026-09-04 补登先例)。
    ShadowItem(
        id="sltp_verify_shadow",
        name="SL/TP 验证影子 (LLM×代码分歧校准)",
        status=ShadowStatus.OBSERVING,
        description="reprice 决定最终 SL/TP 的瞬间记录 LLM 原始值 vs 代码波动率分桶值的分歧 "
                    "(SL_TOL 0.2/TP_TOL 0.3 一致口径), 连同 regime/bucket/atr_pct 落盘, "
                    "供与 pnl 联合校准。看板段3 数据面; record-only, feature flag 控制。",
        data_path="data/shadow/sltp_verify_shadow.jsonl",
        config_key="llm_features.sltp_verify_shadow.enabled",
        min_n=0,
        graduation_criteria="未预注册 — 生产者头部仅自述目的「回答分歧时该信 LLM 还是信代码」。"
                            "若要裁决须先立 DEB 预注册数值门。",
        eval_script="scripts/sltp_shadow_pnl_report.py",
        created="2026-07-12",
        eval_on_demand=True,
        notes="补登 (2026-09-26 RFC-109②): 看板段3 复用 report 脚本 import (7/17 独立 --push "
              "已撤, crontab 注释在案; board 06:15 cron 每日 import 调用 = 实际挂载面, "
              "哨兵按 .py 字面量匹配不到 import ⇒ eval_on_demand 豁免)。写方=pipeline reprice 埋点。",
        no_adjudication_reason="NO_CRITERIA: 无预注册数值门 (补登不得事后造判据) — 裁决前先立 DEB",
    ),
    ShadowItem(
        id="exec_gate_audit",
        name="下单前统一收口审计 (全来源)",
        status=ShadowStatus.OBSERVING,
        description="每个到达执行漏斗的候选 (DA/UE/内部全覆盖) 的来源+最终动作+EdgeShadowGate "
                    "判定 (pipeline._emit_exec_gate_audit, 2026-07-17)。看板段9 数据面: "
                    "候选=执行+跳过+延后 恒等 + BLOCK/PASS 结局回填两窗对照。",
        data_path="data/shadow/exec_gate_audit.jsonl",
        config_key=None,
        min_n=0,
        graduation_criteria="无毕业门 (永久测量项): edge 门本体 DEPRECATED-SHADOW (DEB-20260723 "
                            "C-1: 唯一特征 confidence 已实证反指, 永不激活为 live gate; 任何激活"
                            "提案须先过 INTC 测试+独立 DEB)。本产物=执行漏斗审计留痕 + per-trade "
                            "重建的对照基线。",
        eval_script=None,
        created="2026-07-17",
        notes="补登 (2026-09-26 RFC-109②): 看板段9 自带结局回填/两窗方向合成读数 (9/23 批)。"
              "写方无条件落盘 (审计面, 非假设攒样)。",
        no_adjudication_reason="PERMANENT_MEASURE: 审计留痕面, 无毕业门 (edge 门 DEPRECATED-SHADOW, DEB-20260723 C-1)",
    ),
    ShadowItem(
        id="exit_param_shadow",
        name="退出参数影子 (更紧 breakeven/drawdown 变体 capture 对照)",
        status=ShadowStatus.OBSERVING,
        description="盘中每轮巡检用与 _compute_dynamic_effective_sl 一致的纯逻辑回放【比现行更紧】"
                    "的变体 (be/dd/profit_override/dead_signal/30min-abort/early_tight), close 时"
                    "对比各变体 vs 实盘 capture_ratio。看板段7 数据面 (净口径=毛−0.08% 往返费)。",
        data_path="data/shadow/exit_param_shadow.jsonl",
        config_key="llm_features.exit_param_shadow.enabled",
        min_n=30,
        graduation_criteria="exit_shadow_graduation.py 头部预注册逐字 (2026-07-20): 毕业门禁"
                            "(三项全过=PASS): 1.样本量 N≥30 2.均值改善: 变体 capture > baseline "
                            "capture (或 counterfactual_saved>0) 3.一致性: 变体优于 baseline 的"
                            "比例≥55%。n 轴=**每变体**配对样本, 非 jsonl 总行数。",
        eval_script="scripts/exit_shadow_graduation.py",
        created="2026-07-13",
        eval_on_demand=True,
        notes="补登 (2026-09-26 RFC-109②): 写方=app/playbook/exit_shadow.py (feature flag 控制)。"
              "eval 无 cron (按需/裁决日跑, 哨兵豁免)。⚠️ 与 breakeven_aw_variants 同族不同轴: "
              "本项测多变体 capture, 彼项测 A/W 武装线反事实 — 裁决不互替。",
    ),
    ShadowItem(
        id="universe_expansion_shadow",
        name="选股宇宙扩张影子 (板块轮动→宇宙外异动)",
        status=ShadowStatus.KILLED,
        description="轮入板块→拉成分股→筛宇宙外异动名 capture, 次日 score 前瞻收益+命中率 "
                    "(验证「看得见能源轮入却打不着」缺口)。看板段8 数据面 (horizon 拆批: "
                    "日常批≤3日 vs 回填/长窗批, 9/23 起分印不合并)。",
        data_path="data/shadow/universe_expansion_shadow.jsonl",
        config_key=None,
        # min_n 轴差 (2026-09-26 补判据时显式标注, 防 current_n 被当裁决证据读):
        #   registry current_n 轴 = count_mode `where:type=capture` ⇒ **capture 行数** (供给面);
        #   判据裁决 n 轴 = **horizon==1 日常批的 score results 条数** (证据面)。两者不同源。
        #   裁决一律以 eval 产物 (universe_expansion_eval_latest.json) 为准, current_n 只作供给
        #   进度参考。现读数: capture 49 行 / h=1 results 98 条 ⇒ 双轴均已过 30, 不会误判。
        min_n=30,
        graduation_criteria="预注册 (2026-09-26 用户裁「补判据」; 判据**逐字借用** "
                            "scripts/ue_midday_shadow_eval.py §13.3 = 2026-08-14 用户裁, 早于本轮"
                            "读数 ⇒ 不自创阈值, 遵守阈值购物禁令): ≥10 交易日 ∧ n≥30 ∧ "
                            "命中率(方向调整后前瞻 ≥+0.5%) ≥55% ∧ 均值 ≥+0.5% → 提请用户裁决开启 "
                            "inject (带全套既有守卫); **否则 → 退役** (删 cron + 开关置 false, 零残留)。"
                            " 🔴 裁决口径 = **horizon==1 日常批**; h 2-3 与 h>=5 只披露**不参与裁决** "
                            "(防多重比较挪门柱, 与 ue_tier_gate「方向分层只并列呈报不另设门」同纪律)。"
                            " 为什么必须分批: 写者 8/26 首启一次性回填 26 个 capture 日 (horizon 1..43), "
                            "与日常批 (1-3 天) 混写同一文件 ⇒ 按 scored_on 聚合会把长窗走势当 1 日读数, "
                            "**实测把 LONG/SHORT 方向结论整个翻转** (混批 LONG +2.089% vs h=1 LONG "
                            "−2.043%, 符号相反); 且实测 **h>=5 回填批 LONG 会全过本判据** (命中率 58.1% / "
                            "均值 +3.453% / p=0.048) ⇒ 不分批则判据上线即被污染批假阳性通过。"
                            " ⚠️ 单位换算陷阱: midday 脚本 fwd 是**小数比率** (HIT_THRESHOLD=0.005), 本表 "
                            "fwd_pct 是**百分数** (写者 HIT_PCT=1.0 即 +1.0%) ⇒ 本判据阈值取 **0.5 百分点**; "
                            "命中率一律按 fwd_pct>=0.5 **自算**, 不用写者 hit 字段 (其阈值 1.0% ≠ 判据 0.5%)。"
                            " 阈值稳健性已验: 0.5% 与 1.0% 双阈值对 ALL/LONG/SHORT **同判** ⇒ 结论对阈值"
                            "选择不敏感 (排除「挑了宽松阈值才 FAIL」的可能)。"
                            " ⚠️ verdict 三态 (非二态): 样本不足 → KEEP_SAMPLING (真需攒); 样本足∧效果过 → "
                            "PASS_TO_USER_ADJUDICATION; 样本足∧效果不达标 → **FAIL_CRITERIA_MET** (判据已可裁, "
                            "提请退役裁决)。写成「全过→PASS 否则→KEEP_SAMPLING」会让该项**永久停在攒样** = "
                            "用无限攒样规避裁决, 与僵尸尺子同族。",
        eval_script="scripts/universe_expansion_shadow_eval.py",
        created="2026-07-14",
        last_evaluated="2026-10-22",
        verdict_history=[
            {"date": "2026-09-26", "verdict": "FAIL_CRITERIA_MET (样本足∧效果不达标 ⇒ 判据已可裁, 处置待用户裁)",
             "evidence": "补判据当日首跑 (scripts/universe_expansion_shadow_eval.py, 裁决桶 h=1): "
                         "ALL n=98/19 capture日 命中率 38.8% (门 55%) ✗ 均值 −0.481% (门 +0.5%) ✗ p=0.9902; "
                         "LONG n=33/11日 命中率 **12.1%** 均值 **−2.043%** p=1.0000; "
                         "SHORT n=65/16日 命中率 52.3% 均值 +0.312% p=0.4022 (均未过门)。"
                         " 样本轴双过 (n 98≥30, 日 19≥10) ⇒ 非「攒不够」而是「效果不达标」。"
                         " ⚠️ 混批口径 (report 汇总段现输出) 会给 LONG +2.089% / SHORT −2.108% = **反向**结论, "
                         "禁用。披露桶 h>=5 回填批 LONG 命中率 58.1%/均值 +3.453% 会假阳性过门, 已隔离。"
                         " 🔴 **裁决适用范围 (2026-09-26 同日自纠, 撤回原 overclaim)**: 原 evidence 写「实盘 UE "
                         "通道 LONG 净 −$841 / SHORT 净 +$276 ⇒ 与 h=1 同向, 两条独立证据链一致」— 数字本身正确 "
                         "(decision_id LIKE 'universe_expansion%' 口径 86 笔实算 LONG −$841.25 / SHORT +$275.98), "
                         "但**「独立证据链一致」不成立, 已撤回**: ①本影子测的人群 = 「板块轮入→拉成分股→**宇宙外"
                         "浮现名**」, 61 只以板块成分蓝筹为主 (ABBV/ABT/BA/COST/CVX/DIS/DOW…); 实盘 UE 通道成交的 "
                         "是另一批票 (ADBE/ARM/ASML/AVGO/CRWD/DDOG/BIDU…) ⇒ **人群不同**。②实测重叠 (symbol 归一化 "
                         "去 `US.` 前缀后) 仅 **9 只标的 = 占 UE 14% / 占影子 15%**, 按笔数 **20/86 = 23.3%**; "
                         "未归一化前缀时误得 0% (本仓 `US.` 前缀 join 陷阱, 我自己也踩了一次)。③重叠子集 LONG 净 "
                         "−$180.88 (n=13) vs **非重叠**子集 LONG 净 −$660.37 (n=38) ⇒ 实盘 UE LONG 的出血主要在"
                         "影子**没浮现过**的标的上, 两者不是同一回事。⇒ **本 FAIL 只证明「宇宙外扩张浮现能力」"
                         "无预测力 (前瞻命中率 38.8% < 55%, LONG 腿 12.1%/−2.043% p=1.0), 不能外推成「UE 通道该停」"
                         "或「UE LONG 该禁」**; 后者须用 decision_id 归属的 86 笔实盘盈亏单独走判据 (已在 "
                         "ue_tier_gate 台账留「方向分层只并列呈报不另设门」的备证, 防多重比较挪门柱)。"
                         " ⚠️ 归属口径教训: 查 UE 通道必须用 `decision_id LIKE 'universe_expansion%'`, "
                         "用 `strategy_id LIKE 'universe_expansion%'` 得 **0 笔** (strategy_id 实为 playbook_sim / "
                         "extended_hours_entry 落库标签) — 首次查询即因此得出「0 笔」的错误前提。"},
            {"date": "2026-09-26", "verdict": "KILLED (判据退役已执行: score+eval cron 已摘; capture 保留 — 见 evidence)",
             "evidence": "用户裁「退役」, 按本判据原文「否则 → 退役 (删 cron + 开关置 false, 零残留)」执行。"
                         " 🔴 **执行时实测发现原文两处与仓内现实不符, 已按实测更正 (判据阈值一字未动, 非放宽)**: "
                         "① 「开关置 false」**无处可置** — 写者 scripts/universe_expansion_shadow.py 不读任何 config 键 "
                         "(grep `_cfg|enabled` 无命中), config `universe_expansion:` 段下亦无该影子专属开关 ⇒ "
                         "摘 cron 行是本影子**唯一**真停采机制 (与本文件 collection_retired 字段注释列的先例②同型, "
                         "如 overnight_earnings_carry 靠删 cron 冻结产物)。已摘: score `10 7 * * 2-6` + "
                         "eval `40 7 * * 6` (crontab 357→355 行, 备份 "
                         "cron_backup_before_ue_retire_20260926_213518.txt)。"
                         " ② 「零残留」**不可达** — capture `30 12 * * 1-5` **必须保留**: "
                         "llm_universe_scan.py `_gather_market_corroboration` 的 fallback 只读本文件 "
                         "`caps = [r for r in recs if r.get(\"type\") == \"capture\"]`, "
                         "而 DEB-20260914 §一 P1-3 已实测: UE 的 da_feed 窗 (ET 07:40) **结构上永远早于** "
                         "生产者 DA (ET 08:00) ⇒ 该窗**次次落到 capture 回退**; 盘中脉搏在生产两窗 "
                         "(ET 07:40/09:10) **结构上永不成立** (sector_pulse.py 在 ET 10:00-15:30 才写) ⇒ "
                         "capture 是 UE **生产 prompt「市场佐证」段的常态原料**, 不是纸面观察物。"
                         " 且 crontab 有 `llm_universe_scan.py inject` ∧ config `entry_enabled: true` ⇒ "
                         "UE 真下单 ⇒ **摘 capture = 改 UE 交易行为, 超出「判据退役」的授权范围**, 故不摘。"
                         " 这正是本文件 collection_retired 字段注释预警的形态: 「判据退役 (KILLED/FAIL) "
                         "**不等于**没有消费者 — 实锤 capital_flow 的原料在喂生产门」。"
                         " ⇒ 同理**不设 collection_retired=True**: 该字段语义是「不再采集」, 而 capture 恰恰要"
                         "继续采 ⇒ 设 True 会变成注释警告的「假停采: 产物照样每天写, 看板显示已停采并**抑制停摆"
                         "告警** ⇒ 账面说停了、实际没停、还把证据藏起来 (活谎言结构, 比不设更危险)」。"
                         " 连带面已核: score 行冻结在 2026-09-25, KILLED ∈ shadow_board `terminal` 判定 ⇒ "
                         "跳过「生产者停摆」告警 (不刷屏); capture 仍日更 ⇒ mtime 新鲜。"
                         " 🔴 **本裁决未处置**: UE 交易通道本身 (LONG 净 −$841 / SHORT 净 +$276 的方向不对称) "
                         "**不在本裁决范围** (见上一条 evidence 的适用范围自纠: 人群重叠仅 9 标的 / 23.3% 笔数) ⇒ "
                         "不得据本 KILLED 停 UE 通道或禁 UE LONG; 该问题归 ue_tier_gate 台账「方向分层只并列呈报"
                         "不另设门」的备证线, 须用 decision_id 归属的 86 笔实盘盈亏另立预注册判据。"},
            {"date": "2026-10-22",
             "verdict": "USER_REVIEWED_RETAIN_CAPTURE_CRON (选项 A: 保留, 不改 cron)",
             "evidence": "用户要求退役 12:30 CST capture cron; 删前核对发现 "
                         "`llm_universe_scan._gather_market_corroboration` 的 fallback 只读 "
                         "type=capture (今日 08:00 UE 日志实走 'capture 回退'), KILLED 判据只覆盖 "
                         "影子预测值, 不覆盖 capture 作为 UE/DA prompt 市场佐证原料的角色 ⇒ "
                         "摘 cron = 改 UE 输入行为. 用户选 A: **保留采集**; crontab 第 91 行未动; "
                         "registry 记录复核结论, 无 cron/行为变更. 复核: 2026-10-22."},
        ],
        notes="补登 (2026-09-26 RFC-109②): capture cron 30 12 * * 1-5 + score cron 10 7 * * 2-6 "
              "(8/26 接电)。count_mode=where:type=capture (score 行是派生读数不另计样本)。"
              " 2026-09-26 用户裁「补判据」: 判据逐字借用 ue_midday_shadow_eval (2026-08-14 用户裁) + "
              "新建 eval 执行体; **清空 no_adjudication_reason** (原 NO_CRITERIA 非空会永久屏蔽 "
              "refresh_counts 的 READY 提升与重看窗 — 见 early_dead_signal_shadow 块同型教训, 有判据后"
              "必须清, 否则判据不上账 = 攒着永远没有裁决日)。"
              " ⚠️ 尺子残留缺陷 (零行为变更, 未修): 写者 cmd_report **汇总段** (all_fwd/all_hits, "
              "line 380-394) 仍混 horizon 批 ⇒ 顶部「命中率/平均前瞻收益」读数被长窗回填批污染 "
              "(现印 34.9%/+0.023%, 正确 h=1 口径是 38.8%/−0.481%); 逐日行自 9/23 起已正确标 "
              "[daily/backfill,Nd] 批次。裁决一律走 eval 产物, 勿读 report 汇总段。"
              " ✅**2026-09-26 用户裁「退役」已执行 (FAIL_CRITERIA_MET → KILLED)**: "
              "score cron `10 7 * * 2-6` + eval cron `40 7 * * 6` **已摘** (crontab 357→355); "
              "**capture cron `30 12 * * 1-5` 保留** — 它是 UE 生产 prompt 佐证段的常态原料, "
              "摘它=改交易行为超授权 (详见 verdict_history 同日 KILLED 条 evidence ②)。"
              "写者无 config 开关 ⇒ 摘 cron 是唯一真停采机制; **不设 collection_retired** "
              "(语义与 capture 继续采矛盾, 设 True = 假停采+抑制告警+藏证据)。"
              "判据原文「开关置 false, 零残留」按实测更正为「摘 cron, capture 因生产依赖不可摘」。"
              "产物一律保留不删 (jsonl 仍是 UE 佐证原料; eval 产物留档为反证)。"
              " ⚠️ 勿复活: 重启 score/eval 须先有新预注册判据 (本判据已裁 FAIL, 复用=阈值购物); "
              "要评 UE 通道方向不对称走 ue_tier_gate 线, 别拿本影子的纸面人群当证据。"
              " 2026-10-22 用户复核选项 A: **保留 capture cron** (生产消费方实锤; crontab 未动).",
        count_mode="where:type=capture",
    ),
    ShadowItem(
        id="early_dead_signal_shadow",
        name="死单早期识别影子 (15/30min MFE 快照)",
        status=ShadowStatus.OBSERVING,
        description="持仓 15/30min 检查点 MFE 快照 (30min<0.2%=候选死单 / 15min<0.15% 辅助), "
                    "settler 配对 trade_records 终局回填 settled/is_dead/final_pnl_net。"
                    "看板段10 数据面 (含结算面守卫行, 9/23 批)。DEB-20260811。",
        data_path="data/early_dead_signal_shadow.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="early_dead_signal_report.py 头部预注册逐字: 攒样≥30 命中后评估: "
                            "①双窗口死单率均≥55% ②命中组实际净PnL<0 ③差集结局为负。"
                            "死单定义=exit_reason∈{TIME_EXIT, MFE_STALL_EXIT} (预注册词表, 判决"
                            "口径不动)。⚠️ 词表漂移已在案 (2026-09-23): 预注册词表现役基率仅~1%, "
                            "现役词表三档只印不判; 改判据须另立 DEB。",
        eval_script="scripts/early_dead_signal_report.py",
        created="2026-08-11",
        notes="补登 (2026-09-26 RFC-109②): 写方=exit_engine 检查点埋点 (相对路径陷阱已修, "
              "engineering-change-protocol 2026-09-21 实锤批)。settler cron 2 6 * * 2-6 (9/22 补挂)。"
              "count_mode=where:settled=True — 判据 n 轴是**已结算命中**, 非总行数。",
        count_mode="where:settled=True",
    ),
    ShadowItem(
        id="premarket_peer_shadow",
        name="盘前存储峰群影子 (MU/SNDK/WDC/STX 同向+放量)",
        status=ShadowStatus.KILLED,
        description="富途盘前趋势+成交量对存储峰群做「同向+放量」双条件确认, 与 DA 指令方向对照 "
                    "join pnl_pct (假设: 峰群确认时胜率/收益更好? 背离时更该 SKIP?)。看板段6 数据面。",
        data_path="data/shadow/premarket_peer_shadow.jsonl",
        config_key=None,
        min_n=0,
        graduation_criteria="未预注册 — 生产者头部仅列假设问句, 无数值门。裁决前先立 DEB 预注册 "
                            "(打法自述同 divergence_arbitration: 先攒证据, 有 edge 再切执行前置闸门)。",
        eval_script="scripts/premarket_peer_shadow.py",
        created="2026-07-13",
        last_evaluated="2026-10-03",
        verdict_history=[
            {"date": "2026-10-03", "verdict": "KILLED (判据面 7/22 已证伪; 采集转生产输入, 同 universe_expansion 先例)",
             "evidence": "判据面 (峰群确认→跟随) 7 天回测 **-8.48%** 已于 7/22 证伪 "
                         "(llm_universe_scan.py `_gather_peer_surge` docstring 载明); 此后注册表一直挂 "
                         "NO_CRITERIA「待立 DEB」⇒ 判据无预注册、裁决无出口 = 本清理批要治的攒样无目标形态。"
                         "**采集保留**: premarket_peer_shadow.jsonl 是 UE 盘前 clue `_gather_peer_surge` "
                         "(evidence_ref=peer_surge) 的**唯一数据源** (scripts/llm_universe_scan.py:1441-1449), "
                         "摘 cron = 改 UE 交易行为超授权 — 与 universe_expansion_shadow 9/26 KILLED 同型: "
                         "判据退役 ≠ 采集退役。故不设 collection_retired (语义矛盾); 两条 cron 维持。"},
        ],
        notes="补登 (2026-09-26 RFC-109②): cron 5 21 * * 1-5 采集 + 12 6 * * 2-6 --refresh-join "
              "回填 pnl。按 date upsert ⇒ count_mode=distinct:date (行数=交易日数)。",
        no_adjudication_reason="PERMANENT_MEASURE: 判据已退役 (7/22 证伪), 采集喂 UE 生产 prompt "
                               "(peer_surge clue) — 与 universe_expansion_shadow 同型, 无毕业门",
        count_mode="distinct:date",
    ),
    ShadowItem(
        id="premarket_reprice_shadow",
        name="隔夜仓盘前重定价影子",
        status=ShadowStatus.KILLED,
        description="盘前价格信号 → be_raise/tighten/hold 建议, 次轮用完整日K与实际"
                    "平仓结局结算, 回答「持有中保护线是否应盘前动态重定价」。",
        data_path="data/premarket_reprice_shadow.jsonl",
        min_n=30,
        graduation_criteria="RFC-067 预注册: n≥30 settled 且 be_raise/tighten 各≥5, "
                            "相对 hold 基线有可复现净改善才提案转正; 否则关门。",
        created="2026-08-18",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-03", "verdict": "KILLED (无可复现净改善)",
                          "evidence": "settled=58: hold 26 / tighten 15 / be_raise 17。"
                                      "盘前价离场反事实差: hold -$756.83 (-$29.11/笔), "
                                      "tighten -$397.72 (-$26.51/笔), be_raise +$31.04 "
                                      "(+$1.83/笔); be_touched=15/17。be_raise 微弱正差 "
                                      "不足支撑行为变更, tighten/hold 均负。"}],
        notes="2026-10-03 cron 21:02 行已注释退役, 数据留档; 无服务重启/交易行为变更。"
              "count_mode=where:settled=True — 判据 n 轴为已结算行。",
        count_mode="where:settled=True",
    ),
    ShadowItem(
        id="regime_divergence_shadow",
        name="regime 判定分歧率对比器",
        status=ShadowStatus.KILLED,
        description="同一时刻采集 7 套 regime/市场状态模块, 预注册 n≥10 交易日后按分歧率"
                    "裁「合一」或「重命名消歧」。",
        data_path="data/shadow/regime_divergence.jsonl",
        min_n=10,
        graduation_criteria="DEB-20260726 §R2-5c: 分歧率<10% ⇒ 冗余合一; 分歧率高 ⇒ "
                            "重命名消歧。前提是各模块同输入/同语义可比较。",
        created="2026-07-26",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-03", "verdict": "KILLED (尺子失真, 不构成判据读数)",
                          "evidence": "54 行/51 交易日 all_agree=False; 但仅 2 套产出标签, "
                                      "MarketRegimeDetector.detect() 无参调用使用默认占位 "
                                      "(DEB-20260819-symbol-pangan §43.5)。无活消费者。"}],
        notes="2026-10-03 cron 23:00 行已注释退役; 数据留档。count_mode=distinct:date。",
        count_mode="distinct:date",
    ),
    ShadowItem(
        id="reentry_gate_counterfactual",
        name="再入场证伪门 T+5 反事实尺子",
        status=ShadowStatus.KILLED,
        description="读 intraday_reentry_shadow 被拒行, 用日K算 T+5 方向带符号收益并按"
                    "拒因聚合, 度量证伪门是否误拦赢家。描述性证据, 不构成启停判据。",
        data_path="data/gate_cf_latest.json",
        min_n=0,
        graduation_criteria="描述性遥测, 无转正门 (docs/design/2026-08-22_execution_accuracy_blueprint.md)。",
        eval_script="scripts/gate_counterfactual_ruler.py",
        created="2026-08-22",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-03", "verdict": "KILLED (输入源已停供, 冻结重算空转)",
                          "evidence": "intraday_reentry_shadow 最新行 2026-09-16 "
                                      "(intraday_reentry.enabled=false 后停供); 10/2 仍重算 "
                                      "同一份 3756 拒/1770 可评, 与 reentry_gate_effect_tracker "
                                      "2026-09-23 冻结同型。"}],
        notes="2026-10-03 cron 06:56 行已注释退役; 产物留档。若将来 intraday_reentry 重启, "
              "须先复活本尺并重开影子窗。",
    ),
    ShadowItem(
        id="direction_counterfactual_ruler",
        name="方向反事实尺子",
        status=ShadowStatus.KILLED,
        description="指令级方向×周期矩阵 + 候选级反事实对侧视图, 回答方向是否稳定偏向/"
                    "有 edge。预注册 45-55% 为掷币带, 只产读数不设门。",
        data_path="data/direction_counterfactual_ruler_latest.json",
        min_n=30,
        graduation_criteria="DEB-20260816 §19.2: n≥30 后, 命中率<45% 反指 / 45-55 掷币带 / "
                            ">55 正指; 只作方向裁决读数。",
        eval_script="scripts/direction_counterfactual_ruler.py",
        created="2026-08-20",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-03", "verdict": "KILLED (原始问题已答, 产物无活读者)",
                          "evidence": "BUY 4 档均掷币带; SELL_SHORT 4 档 3 档掷币带, "
                                      "唯一 intraday 55.7% 正指但 dir_mean=-0.016%。"
                                      "产物全仓无代码读者, 仅历史 DEB 引用。"}],
        notes="2026-10-03 cron 06:50 行已注释退役; latest/history 留档, 可手动复跑。",
    ),
    ShadowItem(
        id="t5_hold_counterfactual",
        name="T+5 摆动持有反事实尺子",
        status=ShadowStatus.KILLED,
        description="对已平仓 PLAYBOOK 交易反事实持有至 T+5 收盘, 同费配对比较实际净额, "
                    "供「更长持有期」赛道判读参考。",
        data_path="data/t5_hold_cf_latest.json",
        min_n=30,
        graduation_criteria="DEB-20260822 §四: 描述带 Δ/笔 ≥+$10 支持 / ±$10 中性 / "
                            "≤-$10 不支持; 不替代任何 GO/PIVOT/STOP 判据。",
        eval_script="scripts/t5_hold_counterfactual.py",
        created="2026-08-22",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-03", "verdict": "KILLED (不支持摆动, 产物无活读者)",
                          "evidence": "507 笔: actual -$2,038.38, T+5 CF -$13,436.21, "
                                      "Δ-$22.48/笔, 中位 Δ-$13.80, CF 胜率 47.7% vs "
                                      "实际 58.4%。"}],
        notes="2026-10-03 cron 06:53 行已注释退役; latest/history/SPY cache 留档。",
    ),
    ShadowItem(
        id="da_true_reverse_shadow",
        name="多LLM真反向影子 (阶段0)",
        status=ShadowStatus.OBSERVING,
        description="扫描 DA checkpoint, 登记分析师与独立挑战者 BUY↔SELL_SHORT 真反向"
                    "及反证/失效/翻向条件; Phase 0 record-only, 不改仲裁和交易路径。",
        data_path="data/shadow/da_true_reverse.jsonl",
        min_n=5,
        graduation_criteria="RFC-125 §5: 连续≥5 交易日正常落盘, 非空 decision_id 多行=0, "
                            "checkpoint 与事件一致, 写失败率<1%; Phase 1 enforcement 另需用户裁。",
        created="2026-10-01",
        last_evaluated="2026-10-19",
        stale_expected=True,
        no_adjudication_reason="PHASE0_READOUT: 4 events/4 unique event_id; artifacts 4/4; cron logs 10/07-10/16 无 error; Phase1 enforcement 待用户裁.",
        notes="cron 20:50 * * 1-5 幂等补扫; 当前 2 条独立真反向 (TSLA 9/23 / ORCL 9/29)。"
              "事件行数≠交易日健康行, 故不用计数器自动 READY。"
              "2026-10-19 只读核对: 4 events / 4 unique event_id (TSLA 9/23, ORCL 9/29, "
              "UMC 10/5, SKHY 10/16); artifacts 4/4 存在, checkpoint 含 _ic_decision_id; "
              "cron logs 10/07-10/16 无 error; 事件稀疏≠断流, 故 stale_expected=True。",
    ),
    ShadowItem(
        id="ue_confirm_gate",
        name="UE 开盘确认门 (U11)",
        status=ShadowStatus.OBSERVING,
        description="llm_universe_scan confirm 每交易日裁决 PRICE_IN/CONFIRM/REVERSED, "
                    "结算器 join fwd_ret_t1 判断确认门是否应接为 inject 前置闸。",
        data_path="data/shadow/ue_confirm_gate_report_latest.json",
        min_n=30,
        graduation_criteria="≥10 交易日 ∧ n≥30 ∧ PRICE_IN 前瞻≤0 ∧ CONFIRM 前瞻>0 → "
                            "READY_TO_ADJUDICATE; 任一不满足 → KEEP_SAMPLING。",
        eval_script="scripts/settle_ue_confirm_gate.py",
        created="2026-08-26",
        last_evaluated="2026-10-22",
        notes="采集 cron 21:40/22:40 由 llm_universe_scan.py confirm 承担; 结算 cron 06:04。"
              "当前读数 KEEP_SAMPLING (39 交易日/171 裁决; PRICE_IN +5.52%, CONFIRM -0.09%), "
              "10/31 重看。2026-10-22 用户确认 B: 保留 21:10 scan + score; confirm 按预注册"
              " 10/31 重看; cron 未动。count_mode=json_key:n_verdicts_total。",
        count_mode="json_key:n_verdicts_total",
    ),
    ShadowItem(
        id="short_funnel_shadow",
        name="空头转化漏斗影子打标 (评级看空→指令→成交 同轴链)",
        status=ShadowStatus.OBSERVING,
        description="盘后解析 DA stderr 日志: 深度分析评级分布 × playbook 指令方向对照 → "
                    "空头转化漏斗。2026-09-26 (RFC-109②-A) 修轴: 原 analyzed_n→rated_bearish→"
                    "short_instructions→executed 是**不同分母相除** (rated_bearish=评级看空股票数, "
                    "short_instructions=当日全部 SELL_SHORT 指令数, 无 JOIN) ⇒ 现按 (date,symbol) "
                    "同轴 JOIN 出 rated_bearish→bear_with_short→bear_short_exec, 并分列旁路空头 "
                    "(short_from_non_bearish)。原「三问gate痕迹」列已删 (正则三问|逼空|催化剂时效 "
                    "在 1853 条 skip_reason 命中 0 = 死测量), 换真实拦截原因分布。看板段13② 数据面。"
                    "8/16 用户裁「真正有空头产出是最核心问题」。",
        data_path="data/short_funnel_shadow.jsonl",
        config_key="playbook.short_funnel_shadow_enabled",
        min_n=10,
        graduation_criteria="unified_config.yaml 预注册注逐字 (2026-08-16): n≥10 交易日再裁分层 "
                            "gate。判据达成 ⇒ 处置待用户裁 (立分层 gate 提案须另走 DEB / 或退役)。",
        eval_script="scripts/short_funnel_shadow_eval.py",
        # eval_on_demand=True (2026-09-26 RFC-109②-A): 本 eval 是**待裁项出读数用**的按需评估器
        # (判据 n≥10 已达标, 读数不随日变化, 只在裁决日/周检时跑), 不挂 cron — 同 sltp_verify_shadow
        # (「报告脚本无 cron, 手动/裁决日触发」) 与 iv_hv_residual_eval 先例。采集侧另有 scheduler
        # AFTER_HOURS 挂载 (short_funnel_shadow_tag.py), 不受本豁免影响。
        eval_on_demand=True,
        created="2026-08-16",
        notes="补登 (2026-09-26 RFC-109②): 写方=scripts/short_funnel_shadow_tag.py "
              "(scheduler AFTER_HOURS 挂载)。按 date 幂等 ⇒ count_mode=distinct:date。"
              "⚠️ 补登时实测 30/10 交易日早已达标而从未入待裁面 = 本批要治的「注册表非 SSOT」实锤。"
              "│9/26 二批 (用户裁 A 案): 建 eval_script=scripts/short_funnel_shadow_eval.py "
              "(只印不判, 「立分层 gate」是 L3 生产变更须另走 DEB) + 修同轴链 + 删死列 "
              "gate_block_hints + 把 gz 轮转日志回算的 26 日同轴链回填进产物 (双守卫校验, "
              "既有键 0 改动)。读数见 ADJUDICATED: 看空→指令转化 56.5% ⇒ 证伪 8/16「断点在 "
              "LLM 分析层转化率低」; 最大漏斗口在评级→指令层 (bridge 未提名 ×17); 旁路空头占 55%。",
        count_mode="distinct:date",
    ),
    ShadowItem(
        id="ue_short_boost_shadow",
        name="UE×SHORT 加码影子 (S0, ×1.25 假设队列) (⛔已退役 2026-10-15)",
        status=ShadowStatus.KILLED,
        description="UE 注入的 SELL_SHORT 指令登记「若 ×1.25 加码」假设队列成员, T+1 与未加码"
                    "同池对照。看板段13③ 数据面。DEB-20260805-supply 终裁② (2026-08-06 用户裁同意)。",
        data_path="data/ue_short_boost_shadow.jsonl",
        config_key="universe_expansion.short_boost_shadow",
        min_n=60,
        graduation_criteria="写方注释预注册逐字 (llm_universe_scan S0 段): 主门 n=60 单侧 "
                            "α=0.025 δ=2pp, 敏感性 Bonferroni n=85, 毕业前不动真仓 "
                            "(停止规则见 DEB-20260805-supply §四.3)。",
        eval_script=None,
        created="2026-08-06",
        notes="补登 (2026-09-26 RFC-109②): 写方=llm_universe_scan 指令写入环 (开关只读一次)。"
              "count_mode=distinct:decision_id (每指令一样本, 防重跑灌水)。"
              "2026-10-15 终裁: 写方链 2026-10-06 随 UE fresh-event 改造退役 (真停采机制=cron 摘除), "
              "产物冻结 54/60 ⇒ 主门永不可达; 用户裁「冻结样本结算后归档」⇒ KILLED, "
              "结算读数见 verdict_history 与 data/ue_short_boost_final_settlement.json。",
        no_adjudication_reason="",
        count_mode="distinct:decision_id",
        collection_retired=True,
        verdict_history=[{"date": "2026-10-15", "verdict": "KILLED (冻结样本结算后归档, "
                          "用户裁「冻结样本结算后归档」)",
                          "evidence": "一次性结算器 scripts/ue_short_boost_final_settlement.py "
                                     "(产物 data/ue_short_boost_final_settlement.json): 54 行冻结 "
                                     "→ 可结算 52 (CRNX/NCSM 9月行缺日K, 如实 missing)。"
                                     "T+1 方向调整: mean=+0.0523% / median=+0.3394% / 胜率 51.9% / "
                                     "26 日簇 t=0.667 / MDE80=2.94pp — 声称效应 ≪ MDE ⇒ 即使主门 "
                                     "可达也无可测 edge; 尾部 15 笔均值 −0.4618% (近段漂移为负)。"
                                     "主门 n=60 永不可达 (写方链 2026-10-06 退役) + 读数无 edge "
                                     "⇒ 归档。注意: 读数是「样本未达门」观测值, 不构成确认/证伪。"}],
    ),
    ShadowItem(
        id="overnight_earnings_carry",
        name="隔夜财报 carry 影子 (⛔已退役 2026-09-24)",
        status=ShadowStatus.KILLED,
        description="EXT 候选池×财报门 cohort 的隔夜 carry 可行性 (record-only 零下单)。"
                    "看板段13① 数据面 (末行系停采前归档, 看板已带退役标)。",
        data_path="data/overnight_earnings_carry_shadow.jsonl",
        config_key=None,
        min_n=3,
        graduation_criteria="生产者头部预注册逐字 (2026-09-22 立): n≥3 交易日 ∧ 过闸上界≥2 只/日 "
                            "⇒ 值得立 DEB 提请放宽门的「同日」语义; 过闸上界持续 <0.5 只/日 ⇒ "
                            "该路不成立, 停采 (删 cron 行)。",
        eval_script=None,
        created="2026-09-22",
        last_evaluated="2026-09-24",
        collection_retired=True,
        verdict_history=[{"date": "2026-09-24", "verdict": "KILLED (彻底停采不重启, 用户明示裁决)",
                          "evidence": "2/2 交易日 n_passes_floors=0 (<0.5/日); 9 月淡季 EXT 候选池∩"
                                      "财报门=∅ 结构性零供给; 判据字面 n≥3 未满足 ⇒ 记提前停采例外 "
                                      "(判据文字零改动), 详见 docs/analysis/"
                                      "2026-09-24-carry-shadow-adjudication.md §2。数据质量披露: "
                                      "190 行明细键全 null 仅 cohort 汇总可用。"}],
        notes="补登终态 (2026-09-26 RFC-109②): 9/24 用户裁时两条采集 cron 行已删 (原文留档裁决"
              "文档附录) 但注册表从未登记 ⇒ 看板段13① 带退役标而 SSOT 无痕。jsonl 保留归档不删; "
              "10 月财报季重启须**重新预注册** (判据/口径/采集字段一并重写), 不得直接恢复 cron。"
              "count_mode=distinct:et_date (行数=轮次, 判据 n 轴=交易日)。",
        count_mode="distinct:et_date",
    ),
    ShadowItem(
        id="edge_gate_shadow",
        name="Edge 闸门影子 (DEPRECATED-SHADOW 对照基线)",
        status=ShadowStatus.OBSERVING,
        description="EdgeShadowGate 按 confidence 桶的风险归一期望 would_pass/would_block 判定 "
                    "落盘 (app/decision/edge_shadow_gate.py)。看板段4 数据面 (edge_weekly_report "
                    "一致率/更严更松)。",
        data_path="data/shadow/edge_gate_shadow.jsonl",
        config_key=None,
        min_n=0,
        graduation_criteria="无毕业门 (DEB-20260723 C-1 裁决逐字): 本门永不激活为 live gate "
                            "(当前形态) — 唯一特征 confidence 已实证反指 (IC 为负), 桶级期望"
                            "±0.001~0.009 噪声级随 latest.json 每日翻转。保留 shadow 记录供 "
                            "per-trade 评分重建 (独立 DEB) 对照基线; 任何激活提案必须先过 INTC "
                            "测试 (07-22 回放不得拦 INTC)。",
        eval_script=None,
        created="2026-07-11",
        notes="补登 (2026-09-26 RFC-109②): 看板段4 复用 edge_weekly_report.build_edge_weekly_summary "
              "(import, 无独立 --push cron)。exec_gate_audit 的 edge_verdict 字段与本产物同源同门。"
              "2026-10-16 (DEB-20261016) 固化复核: exec_gate_audit 的「BLOCK 组更赚」读数不稳健 "
              "(滚动窗 3 正 4 负翻号, PASS 组对结算快照敏感不可复现; 底层 confidence IC≈0, "
              "桶期望逐日翻号) ⇒ 「永不激活」裁决维持并强化; 任何实装提案仍须先过 INTC 测试。",
        no_adjudication_reason="PERMANENT_MEASURE: DEPRECATED-SHADOW (DEB-20260723 C-1 永不激活), 仅作 per-trade 重建对照基线",
    ),
    ShadowItem(
        id="breakeven_aw_variants",
        name="保本 A/W 变体影子 (RFC-108 结算环, 2026-09-26 结案)",
        status=ShadowStatus.KILLED,
        description="A 变体 (武装 0.8% 更早保本) / W 变体 (开盘窗 gating 不武装) 对真实退出的"
                    "反事实对照, era 分层 (be_0.4 裁决层 / be_0.5 描述层)。写方=exit_engine "
                    "保本触发埋点 (3 处), 结算=breakeven_shadow_settle.py (RFC-108 复活批, "
                    "cron 38 6 * * 2-6)。",
        data_path="data/breakeven_shadow.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="结算器 flip_criteria 逐字 (脚本算好落盘, 下游只许照抄): "
                            "n>=30 AND 单侧符号检验 p<0.05 AND 均值>0 — 三条全满足才翻 "
                            "(era=be0.4 为裁决层); W 节同判据评估开盘窗 gating 反事实。",
        eval_script="scripts/breakeven_shadow_settle.py",
        created="2026-07-29",
        last_evaluated="2026-09-26",
        verdict_history=[{"date": "2026-09-26", "verdict": "KEEP_CURRENT_0.4 (A 不翻) + W 开盘窗武装维持",
                          "evidence": "9/26 06:41 结算 (data/breakeven_shadow_settlement.json): "
                                      "A era=be0.4 n=108 mean+0.030pp 中位−0.247 符号检验 p=0.9998 "
                                      "(36 wins/108) ⇒ n✓ 均值✓ p✗ 三条不全过, flip_verdict="
                                      "KEEP_CURRENT_0.4 (脚本判定照抄); W n=32 (be0.4 层 25: "
                                      "mean−0.46pp p=0.9967 胜率 24%) ⇒ gating 反事实为负, "
                                      "不加开盘窗 gating (再证 8/19 P1-b 撤回)。结案=维持 be_0.4, "
                                      "RFC-108 判据口径, 用户批准顺序单第 8 项。"}],
        notes="补登+结案 (2026-09-26 RFC-109②/⑧): 影子本体 7/29 起采集从未入注册表; RFC-108 "
              "(9/25) 复活结算器后今晨首跑即出终局读数。collection_retired=False — 采集+settler "
              "保留作轻量回归监测 (若变体假设重提须新预注册); 停采与否另行裁决, 数据不删。"
              "〔2026-10-22 更正 (全链路复查 R2): 10-21 起 D 臂 (BE 武装点减半 50%) 前瞻实验 "
              "寄生于本 KILLED 项的结算器产物 settlement.json::D — 已独立登记为 "
              "be_half_at_arm_d (见下), 本项维持 KILLED 不变。〕",
    ),
    ShadowItem(
        id="be_half_at_arm_d",
        name="D 臂: BE 武装点减半 50% 前瞻影子 (2026-10-20 批, RFC-154 关联)",
        status=ShadowStatus.OBSERVING,
        description="breakeven 武装触发时记一半仓位的反事实减半结算 (conservative 口径: "
                    "武装点 10bps 不利滑点 + 二次出场费按实际费 25%)。结算器 "
                    "breakeven_shadow_settle.py 的 D 节 (cron 38 6 * * 2-6 每夜), "
                    "历史 161 笔原始读数 +$8.25/笔 / 保守口径 +$3.44/笔 / 胜率 72.0% / 日 t=4.71; "
                    "前瞻自 2026-10-21 起采 (prospective 键)。零交易行为变更; 转正须另立 DEB + "
                    "Playbook 部分平仓执行改造 (当前 close 路径不支持部分平仓)。",
        data_path="data/breakeven_shadow_settlement.json",
        config_key=None,
        min_n=30,
        count_mode="json_key:D.prospective.n",
        graduation_criteria="预注册 (2026-10-20 批, settlement.json::D.flip_criteria 逐字): "
                            "prospective n≥30 AND conservative mean_delta>0 AND 日聚类 t>2 AND "
                            "剔 top3 后均值>0; 历史样本只作假设生成, 不作裁决依据。",
        eval_script="scripts/breakeven_shadow_settle.py",
        created="2026-10-21",
        notes="2026-10-22 补登记 (全链路复查 R2: KILLED 项下藏活跃新实验 = 跟踪/告警盲区)。"
              "读数命令: 看 data/breakeven_shadow_settlement.json::D.prospective "
              "(n/summary_conservative/day_clustered_t_conservative/mean_excl_top3)。"
              "回滚: 删本条目 (采集随 breakeven 埋点存续, 另裁)。",
        cross_ref=("breakeven_aw_variants",),
    ),

    # ── 2026-09-28 DEB-20260928① 用户裁决落地 (§二十 授权代理终裁) ──────────
    ShadowItem(
        id="trail_lock_pyramid",
        name="trail-lock 赢家金字塔加仓影子 (DEB-20260928①)",
        status=ShadowStatus.KILLED,
        description="持仓首次进入 trail-lock 武装态 (breakeven_triggered) 时, record-only 登记"
                    "「帽内可执行加仓腿」: arm 价/qty/equity 全实测 (app/playbook/pyramid_shadow.py, "
                    "monitor 每轮 observe; 去重墓碑=meta['_pyr_armed'] 经既有 metadata 写回持久化), "
                    "武装后逐轮价格轨迹同档。结算=trail_lock_pyramid_settle.py (cron  nightly): "
                    "腿不跨日 (EOD 兜底), 武装前保护=arm 价 (继承母仓净保本线), 自身 trail 复用 "
                    "0.8/1.0, 滑点 0.10% 方向感知 + 费 0.15% 往返, 判据只按帽内可执行 "
                    "add_ratio=min(40%,(cap−arm名义)/arm名义) 结算 (无帽 40% 仅诊断列)。"
                    "零干预: 不碰退出判定/不下单/不写交易表。",
        # 计数轴 = 结算器 latest.json 的 n_executable (帽内可执行武装事件), 不是原始
        # JSONL 行数: 台账逐轮追加 (~12k 行) 而判据 n 轴是事件 (2026-10-17 审计实锤,
        # 默认 lines 会在 10/03 之前就把 12k 行读成"已达 40" → 虚假 READY)。
        # 同 tests/test_criteria_registry_visibility.py 的既定纪律: 行数≠判据轴时用
        # json_key:<结算器键>; settle 由 cron `41 6 * * 2-6` 每夜刷新该产物。
        data_path="data/shadow/trail_lock_pyramid_latest.json",
        count_mode="json_key:n_executable",
        config_key=None,
        min_n=40,
        graduation_criteria="DEB① §七预注册 (读数前写死, 死线 2026-12-31): 仅 post-9/17 config 层; "
                            "n≥40 帽内可执行武装事件 (add_ratio>0) ∧ 虚拟腿净额中位>0 ∧ Σ净额>0 ∧ "
                            "腿回吐中位 ≤ 母仓回吐中位 ∧ 日聚类 t + MDE(80%) 并报 "
                            "→ 另立 DEB 提请上 sim (用户裁); 否则维持现状结题。"
                            "禁改: 40% 上限 / 0.10% 滑点垫 / 独立 trail 复用参数 / add_ratio 公式 — 改=重裁。"
                            "⚠️期望量级预注册 (§十一/§十三): 单事件期望≈$0.5–1.5、n=40 总期望 $20–60, "
                            "MDE 与效应同量级 ⇒ 「测不出」≠证伪; 本影子真正回答的决策问题是"
                            "「武装赢家是否值得单名帽让路」, 非「开 40% 加仓」。",
        eval_script="scripts/trail_lock_pyramid_settle.py",
        created="2026-09-28",
        collection_retired=True,  # 真停采机制: playbook.execution_parameters.pyramid_shadow_enabled=false
                                  # (pyramid_shadow.py observe 首行门控) + settle cron 注释退役 (2026-10-22)
        verdict_history=[
            {"date": "2026-10-22", "verdict": "FAIL_CRITERIA_NOT_MET",
             "evidence": "2026-10-22 用户裁「按判据裁 FAIL」(全链路复查 P1-3)。n 门已达标 "
                         "(n_executable=86 ≥ 40, settle 产物 10-21 18:41 ET) 但 §七判据 4 项仅 1 过: "
                         "腿净中位 −$0.32 (✗>0) / Σ腿净 −$6.60 (✗>0) / 腿回吐中位 0.10% ≤ 母仓 0.83% (✓) "
                         "⇒ 落预注册「否则」分支: 维持现状结题, 不另立 DEB 提请上 sim, 停采。"
                         "与 §十一期望量级并陈: 单事件期望 $0.5–1.5 (n=40 总 $20–60), 实测 Σ=−6.60 "
                         "符号为负, 非「测不出」而是「方向不成立」(测不出≠证伪条款不挡 FAIL: "
                         "该条款只约束「不夸大证伪强度」, 不阻止按判据结题)。数据全部保留不删。"},
        ],
        notes="攒样速率估计 (§十二 更正后): post-9/17 trail-lock 18 笔/8 天 × add_ratio>0 占比 "
              "15/18 ⇒ ~1.875/日, n≥40 约 21 个历日 (按实际计数滚动核)。裁决链: §十五作者复查 → "
              "§十六 Codex → §十七 kimi-k3 (帽内口径实测: 18/18 破帽于1.4×, 均值 4.15%) → "
              "§十九 Codex (方向感知 ±0.4% BE 代理更正: 零余量 3/18)。判据原文见 DEB① 文档。"
              "〔2026-10-22 结案: FAIL_CRITERIA_NOT_MET 停采, 见 verdict_history 与 ADJUDICATED; "
              "n_in_progress=4 行随停采不再结算, 已如实记录。〕",
        intent="",
        cross_ref=(),
    ),

    # ── 2026-09-28 补登 (DEB-20260928② 教训: 未登记 ⇒ P0.5 普查漏查, 重复立案) ──
    #   该影子 7/11 起生产 latest.json 却从未入注册表 (RFC-109② 补登批遗漏),
    #   DEB② 价格分层提案因此重复造轮子后被撤回。机制推导结论与 handoff 边界
    #   ("勿重复实现 / 不下该拦结论") 逐字摘自其产物, 判据照抄 handoff 原口径。
    ShadowItem(
        id="ticket_size_breakeven",
        name="票档盈亏平衡分析 (费用地板/可达名义带)",
        status=ShadowStatus.OBSERVING,
        description="只读机制推导 (非攒样影子): fee_bps ≈ max(39800/N, 262/P) + 0.21 双地板; "
                    "股价 <$20 在任何名义额下压不到 13.3bps 以下; min_entry_price=$5 恰落渐近线 "
                    "52.6bps=全表最贵档; escape_hatches 三选一 (净值缺口 15.6% / 单名帽 6.0→6.94% / "
                    "毛边际提到 7.04bps)。handoff 原文: 档位反事实归口 scripts/admission_replay.py "
                    "(既有, 勿重复实现); 不下「该拦」的结论 — 只给档位算数字。",
        data_path="data/shadow/ticket_size_breakeven_latest.json",
        config_key=None,
        min_n=30,
        graduation_criteria="机制推导已结案 (mechanical_certain), 无攒样判据; 含 "
                            "gross_bps_notional_weighted_excl_top3 剔极值口径 — DEB② 撤回后, "
                            "「≥$100 层剔极值仍正」观察项并入本产物跟踪。",
        eval_script="scripts/ticket_size_breakeven.py",
        # 2026-10-03 清理批: read_only 按需重算器 (无 cron 亦不应挂 cron — 产物只在
        # 人工复核时刷新), 同 sltp_verify_shadow 等 11 条 eval_on_demand 先例。
        eval_on_demand=True,
        stale_expected=True,
        created="2026-07-11",
        notes="补登 (2026-09-28): 产物 since 2026-07-11、read_only。DEB-20260928② (供给价格分层) "
              "因 P0.5 普查未见本项而重复立案, §15.2/§17.3 双重确认后撤回并入本项。",
    ),

    # ── 2026-10-03 清理批 (REVIEW-20261003): 补 SSOT 缺口 + 到期裁决落地 ──────────
    # 立项理由: 交叉审计发现 5 条**在采**影子未注册 (config 开关 true + 产物日更,
    # 注册表查无) ⇒ 周检/待裁面/挂载哨兵全部失明; 其中两条 (cluster/conflict)
    # 判据门早已达标无人裁, 一条 (ue_midday) 预注册判据已 FAIL 未执行退役处置。
    ShadowItem(
        id="shadow_portfolio",
        name="影子组合 (全门被拦指令反事实 P&L 台账)",
        status=ShadowStatus.OBSERVING,
        description="pipeline 对「被门拦下但本会触发」的指令按 (inst_id,date) 去重登记, "
                    "盘后按生产退出栈重放到当日收盘 (settle_shadow_portfolio.py, Hermes "
                    "08:10 no_agent, 空结算静默), 回答「每道门拦掉的到底是钱还是坑」。"
                    "用户口径: 影子组合 = 证伪门/门禁族的统一反事实地基。",
        data_path="data/shadow_portfolio_entries.jsonl",
        config_key="playbook.shadow_portfolio_enabled",
        min_n=0,
        graduation_criteria="永久测量项 — 无毕业门 (反事实台账随门族存续); 单门归因禁用本台账 "
                            "(跨原因去重先到先占, 漏计后发原因), 归因须用 "
                            "trigger_miss_telemetry.jsonl (逐轮按 reason 区分)。",
        eval_script="scripts/settle_shadow_portfolio.py",
        eval_on_demand=True,
        created="2026-08-09",
        notes="2026-10-03 补登记 (清理批): 采集+结算+消费链全活 (ic_weekly_report 读 outcomes; "
              "Hermes 影子组合每日结算 job), 但注册表查无 ⇒ 挂载/待裁面失明。结算 cron 在 "
              "Hermes (shadow_settle_daily.sh) 而非 root crontab ⇒ eval_on_demand=True "
              "(同 sltp_verify 先例)。count_mode=lines (行=已登记被拦指令, 结算端幂等去重)。",
        no_adjudication_reason="PERMANENT_MEASURE: 反事实地基, 无毕业门 (详见判据字段)",
        count_mode="lines",
    ),
    ShadowItem(
        id="cluster_concentration_shadow",
        name="簇浓度影子 (同簇并发开仓帽反事实)",
        status=ShadowStatus.KILLED,
        description="scheduler AFTER_HOURS 重放每日开仓, 按手工簇映射记同簇并发数, "
                    "任意 cap (3/4/5/6) 可离线重切 — 回答「日开仓帽16/并发帽12 之外, "
                    "是否需要结构维度(单簇)真帽」。写方 scripts/cluster_concentration_shadow.py。",
        data_path="data/cluster_concentration_shadow.jsonl",
        config_key="playbook.cluster_concentration_shadow_enabled",
        min_n=30,
        graduation_criteria="写方头部预注册 (逐字, summary.criteria 同步): n≥30日 且 "
                            "blocked≥30笔 且 blocked_pnl净差>0 + 符号p<0.05 才议真簇帽。",
        eval_script=None,
        created="2026-08-27",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[
            {"date": "2026-10-03", "verdict": "KILLED (判据门 44 日已过, 方向反 + 子样本不足)",
             "evidence": "summary (updated 10/02): n_days=44 ✓ (门 30); cap3 blocked=29 ✗ (门 30, "
                         "最激进档仍差 1 笔), 且 blocked_pnl_sum=**+$433.18** — 被拦的是**盈利单**, "
                         "立帽=主动弃利, 与判据『净差>0』方向相反; cap4/5/6 拦得更少 (8/4/2 笔)。"
                         "max_cluster_concurrent_ever=7。三条门 0/3 过 ⇒ 不立簇帽。停采: "
                         "cluster_concentration_shadow_enabled=false (scheduler 读 ConfigContainer, "
                         "热重载可读); 44 日数据 + summary 留档。回滚: true + revert OBSERVING。"},
        ],
        notes="2026-10-03 补登记+结案 (清理批): 未注册 44 天且判据门早已到期无人裁。"
              "采集停后 summary 不再刷新 — 读历史以 verdict 落档日为界。",
    ),
    ShadowItem(
        id="conflict_shadow",
        name="隔夜持仓×新指令冲突影子",
        status=ShadowStatus.KILLED,
        description="W_WEEK 多夜持仓后, 隔夜股次日被 DA/UE 再选中时记录冲突形态 "
                    "(同向拦截/反向翻转/候选浪费)。执行段已有方向感知防御 "
                    "(pipeline 同向拦截/反向放行), 本影子回答「是否还需候选端排除」。"
                    "写方 scripts/conflict_shadow_tag.py (AFTER_HOURS)。",
        data_path="data/conflict_shadow.jsonl",
        config_key="playbook.conflict_shadow_enabled",
        min_n=30,
        graduation_criteria="scheduler 挂载注释预注册 (逐字): n≥30 再裁是否加候选端排除。",
        eval_script=None,
        created="2026-08-16",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[
            {"date": "2026-10-03", "verdict": "KILLED (n=36 达标: 频率低, 排除提案无证据)",
             "evidence": "36 事件/25 交易日 (n≥30 ✓): 同向拦截 28 (既有方向感知防御在位), "
                         "反向翻转仅 5 (其中实际成交 2: HOOD 9/8, ACN 10/2; ACN 结局未平), "
                         "候选浪费 3。翻转频率 ≈0.2/日 (5/36=14%); 成交翻转结局 n=2 不可判。"
                         "⇒ 候选端排除无证据支持, 不提案; 停采: conflict_shadow_enabled=false "
                         "(热重载可读); 36 行留档。回滚: true + revert OBSERVING。"},
        ],
        notes="2026-10-03 补登记+结案 (清理批): 未注册 48 天, n≥30 门早已过无人裁。"
              "既有执行侧防御 (同向拦截/反向放行) 继续在位, 本裁决零交易行为变更。"
              "〔2026-10-22 路径漂移更正 (全链路复查 R1): 本项 data_path 指向 "
              "data/conflict_shadow.jsonl (10-03 起冻结) — 但 RFC-125 新一代冲突影子 "
              "(指令写库边界, conflict_shadow.py::emit_save_conflict_shadow) 在采于 "
              "data/shadow/instruction_conflict.jsonl 且未注册 ⇒ 已独立登记为 "
              "instruction_conflict_shadow (见下), 本项维持 KILLED 终态不变。〕",
    ),
    ShadowItem(
        id="instruction_conflict_shadow",
        name="RFC-125 指令写库边界冲突影子 (多源/多轮冲突矩阵阶段0)",
        status=ShadowStatus.OBSERVING,
        description="playbook_instructions 写库边界 (db.py save 路径) 逐次记录冲突形态: "
                    "same_decision_id 材料冲突 / DA vs UE 同日同标的反向 / reentry 多轮共存 / "
                    "独立真反向 score_gap 分档。阶段0 只记录不拦截 (enforcement 未实施)。"
                    "写方 app/playbook/conflict_shadow.py::emit_save_conflict_shadow "
                    "(无 config 门, 随写库路径常驻)。",
        data_path="data/shadow/instruction_conflict.jsonl",
        config_key=None,
        min_n=5,
        count_mode="distinct:date",
        graduation_criteria="RFC-125 §5 预注册 (阶段1 enforcement 前须同时满足): "
                            "①影子连续 ≥5 个交易日正常落盘 ②非空 decision_id 多行数恒为 0 "
                            "③checkpoint 扫描与落盘事件的独立真反向数量一致 ④影子写失败率 <1% "
                            "且失败不阻断写库/交易路径 ⑤用户显式裁决启用 enforcement。",
        eval_script=None,
        eval_on_demand=True,
        created="2026-10-22",
        notes="2026-10-22 补登记 (全链路复查 R1 在采未注册): 实际在采已久 (产物 "
              "instruction_conflict.jsonl 持续刷新), 注册表只有 KILLED 的旧代 conflict_shadow "
              "(路径已漂移) ⇒ 本项补登。count_mode=distinct:date 对判据① (落盘交易日数); "
              "判据②-④ 的测量脚本待建 (列入裁决前必补)。回滚: 删本条目 (写方不动, 产物留档)。",
        cross_ref=("conflict_shadow",),
    ),
    ShadowItem(
        id="ue_midday_shadow",
        name="UE 午间影子扫描 (12:30 ET 浮现前瞻)",
        status=ShadowStatus.KILLED,
        description="午间独立扫描 (llm_universe_scan.py scan --midday) 落 "
                    "llm_universe_scan_midday.jsonl, 主链路 (inject/push/score/DA供料) 只读主 "
                    "scan 文件, 午间记录零污染。判据: 方向调整后 12:30→收盘前瞻。",
        data_path="data/shadow/llm_universe_scan_midday.jsonl",
        config_key="universe_expansion.midday_shadow_enabled",
        min_n=30,
        graduation_criteria="scripts/ue_midday_shadow_eval.py 头部预注册 (DEB-20260814 §13.3, "
                            "2026-08-14 用户裁, 逐字): ≥10 交易日 且 午间浮现 n≥30 "
                            "(tradeable, 按 symbol+date 去重); 命中率 (ret≥0.5%) ≥55% 且均值 "
                            "≥+0.5% → 开启午间 inject (带全套守卫); 否则: 退役 (删 crontab 行 + "
                            "midday_shadow_enabled=false), 零残留。",
        eval_script="scripts/ue_midday_shadow_eval.py",
        created="2026-08-14",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[
            {"date": "2026-10-03", "verdict": "KILLED (预注册判据双 FAIL, 机械执行退役处置)",
             "evidence": "10/03 09:56 实跑 ue_midday_shadow_eval.py: 10 个交易日 (门 ≥10 ✓), "
                         "tradeable n=41 (可评 40, 门 ≥30 ✓); 命中率 22.5% (门 55% ✗), "
                         "均值 −0.78% (门 +0.5% ✗) ⇒ **双判据 FAIL**。按预注册处置原文执行: "
                         "① cron 午间行 30 0 * * 2-6 已注释 (墓碑留档); ② "
                         "midday_shadow_enabled=false。数据 50 行/35 日留档。回滚: 去 cron 注释 + "
                         "开关 true + revert OBSERVING (改判据=重裁, 须另立 DEB)。"},
        ],
        notes="2026-10-03 补登记+结案 (清理批): 采集 50 天未注册; 判据 8/14 用户裁预注册, "
              "今日实跑读数 FAIL ⇒ 按其自带处置机械退役。此为清理批停掉的唯一**日频 LLM 扫描** "
              "(每日 12:30 ET 一次 scan, 判据已死后继续空跑 = 空耗实锤)。",
    ),
    ShadowItem(
        id="ue_earnings_clue_shadow",
        name="UE 财报 clue 影子 (全市场只记不喂)",
        status=ShadowStatus.KILLED,
        description="llm_universe_scan.py 将全市场财报 clue 落 "
                    "ue_earnings_clue_shadow.jsonl 而不进 clue brief (record-only), "
                    "用于后续评估财报 clue 的边际价值。写方 gated by "
                    "universe_expansion.clue_earnings_shadow (DEB-20260829 第3轮, UE-B1)。",
        data_path="data/shadow/ue_earnings_clue_shadow.jsonl",
        config_key="universe_expansion.clue_earnings_shadow",
        min_n=30,
        graduation_criteria="无 — 立项时只声明「只记不喂」, 从未预注册数值门, 也无 eval 脚本 "
                            "(补登不得事后造判据)。",
        eval_script=None,
        created="2026-08-26",
        last_evaluated="2026-10-03",
        collection_retired=True,
        verdict_history=[
            {"date": "2026-10-03", "verdict": "KILLED (write-only 38 天, 零判据零消费方)",
             "evidence": "交叉审计: 全仓唯一引用 = 写方自身 (scripts/llm_universe_scan.py:1315), "
                         "**零读方/零 eval/零预注册判据**; 产物 100KB 持续增长 38+ 天。按 "
                         "AGENT.md「无消费方的字段一律删 — write-only 是膨胀主因」同纪律与用户 "
                         "2026-10-03 清理令 (减攒样/提信噪比) 退役: clue_earnings_shadow=false "
                         "(热重载可读), 数据留档。回滚: true + revert OBSERVING (届时须先补预注册判据)。"},
        ],
        notes="2026-10-03 补登记+结案 (清理批): 未注册 38 天。若未来重开, 判据必须先立 "
              "(同 premarket_peer 教训: 无判据攒样 = 无出口)。midday 影子同日 KILLED 与本条无关 "
              "(彼有预注册判据, 此无)。",
    ),
    ShadowItem(
        id="premarket_trend_continuation_shadow",
        name="盘前异动延续率 / 顺势影子收益 (C13)",
        # 2026-10-08 用户裁 FAIL·轴关闭 (判据字典 C13, n=665/39 交易日到期即裁)。
        # 五关全不过 + 正读数双重支撑皆伪 (脏行+加密族独占) ⇒ 禁止新建入场信号/prompt 注入。
        # 载体处置: cron 保留 (build_symbol_trading_cards.py 消费 jsonl + A类避损核查仍有效),
        # 采集逻辑/行格式零改动, 仅输出累计行文案标「已裁 FAIL」; jsonl 留档不删。
        status=ShadowStatus.KILLED,
        description="C13: 盘前 |chg|≥4% 顺势 open→close 轴 (v0.64 B类)。n=665/39 交易日, "
                    "五关全不过: ①显著性 日聚类 t=+0.60; ②效应量 mean +0.252% < 0.30% 地板、"
                    "扣费 +0.152%; ③安慰剂 obs ∈ CI95 pct=80%; ④尾部主导 剔 top3 后 +0.052%≈归零; "
                    "⑤方向分层 做多腿 −0.219% (负)、做空腿 +0.838% 但 regime 代理。"
                    "正读数双重支撑皆伪: (a) 8/29 prev_close 修复前脏行 15 笔物理不可能, "
                    "干净窗 −0.122% (t=−0.58); (b) 15% 杠杆/加密 ETF 行独占全部正均值, "
                    "加密族同日齐动 ⇒ 有效 n ≪ 665 且做空无券池=物理不可执行。"
                    "「延续率 94%」不可变现: 延续量收盘vs昨收而影子量开盘→收盘, "
                    "延续✓的 622 笔中 50% 开盘后为负=缺口开盘即 price-in。"
                    "禁止反手使用做空腿 (T28 inversion ban)。",
        data_path="data/premarket_trend_outcomes.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="C13 (预注册, 改=重裁): n≥30 再评估。到期即裁五关: "
                            "①日聚类 |t|≥2; ②扣费 mean≥0.30%; ③安慰剂 obs ∉ CI95; "
                            "④剔 top3 后方向一致; ⑤方向分层同号。"
                            "复活条件 (全部满足): 新论据+新数据源; 人群预过滤到可执行集 "
                            "(排杠杆/加密 ETF、<$5、低流动性, 同族同日不重复计数); "
                            "干净窗扣费 mean≥0.30% ∧ 日聚类 |t|≥2 ∧ 双窗同号; "
                            "与现役通道重叠切分后差集仍有增量。不得引用本轮已裁参数点。",
        eval_script="ops/hermes/premarket_trend_verify.py",
        created="2026-08-12",
        last_evaluated="2026-10-08",
        collection_retired=False,  # 采集保留: build_symbol_trading_cards.py 消费 + A类避损核查
        verdict_history=[
            {"date": "2026-10-08", "verdict": "KILLED (五关全不过, 正读数双重支撑皆伪)",
             "evidence": "DEB-20261008-premarket-trend-continuation-verdict: n=665/39 交易日, "
                         "五关全不过 (详见 description); 正读数双重支撑皆伪: (a) 脏行 15 笔 "
                         "(NBIL/NBIZ day_chg ±68%、SGLD pm+248%), 干净窗 (8/31–10/7, n=437) "
                         "−0.122% (t=−0.58); (b) 杠杆/加密 ETF 独占正均值, 加密族同日齐动 "
                         "(8/21 单日 13 只 XRP/BTC 系) ⇒ 有效 n ≪ 665。in_da 子集 −0.570% ⇒ "
                         "对现役通道零增量 (T14)。禁止反手使用做空腿 (T28 inversion ban)。"
                         "载体处置: cron 保留 (build_symbol_trading_cards.py 消费 jsonl + "
                         "A类避损核查仍有效), 采集逻辑/行格式零改动, jsonl 留档不删。"},
        ],
        notes="2026-10-09 补登记 (C13 治理缺口留痕: 攒样器 57 天从未入 shadow_registry, "
              "周检/哨兵不可见, B21/B22 缺口族第 4 例)。采集维持: cron 2a0eb17a475e 保留 "
              "(build_symbol_trading_cards.py 消费 jsonl + A类避损核查仍有效), 仅输出累计行"
              "文案标「已裁 FAIL」。治理: 不得以本判据新建入场信号/prompt 注入或门。",
    ),
    # ── 2026-10-15 全链路审计 P1-3 对账批: 「在采未注册 = 军规禁止态」补登 ×3 ──
    ShadowItem(
        id="eth_held_exit_shadow",
        name="ETH HELD→RTH 会话升级影子 (RFC-127, 移交失败监测面)",
        status=ShadowStatus.OBSERVING,
        description="ETH 窗内限价腿 TERMINAL 后 intent 卡 HELD、RTH 开盘仍被锁死 (SL 评估被 "
                    "blocked_exits 跳过) 的 record-only 观测 (RFC-127, 2026-10-01 起; 首例行=10-01 "
                    "US.ACN 23 股卡死地面真值)。写方 app/playbook/eth_held_exit_shadow.py "
                    "(build_eth_held_rth_event + emit, 幂等 event_id=ETH-HELD-RTH:sha256(intent_id)), "
                    "接线 pipeline.py:3330 (resume_close 之后, try/except 保险丝)。零行为变更。",
        data_path="data/shadow/eth_held_exit_escalation.jsonl",
        config_key=None,  # 无开关; 真停采只能摘 pipeline 接线
        min_n=3,
        count_mode="distinct:intent_id",
        graduation_criteria="RFC-127 §4 逐字 (阶段1 启用门): ①≥5 个交易日 且 ≥3 个独立 HELD 意图命中 "
                            "(或单意图 SL 越线暴露 ≥$500, 取先到) ②命中意图 100% 满足 RTH开盘+剩余>0+"
                            "无并发ACTIVE腿 ③无害性反证 (升级窗内回落优于补腿成本=0 例) ④单意图 ≤2 次告警 "
                            "⇒ 提请阶段1 行为变更 (须用户二次裁决)",
        eval_script=None,
        eval_on_demand=True,
        stale_expected=True,
        no_adjudication_reason="PERMANENT_MEASURE: 移交失败监测面 (2026-10-15 追认后 §4 原判据失效); 新行即 P1, 无毕业门.",
        created="2026-10-01",
        notes="唯一消费者 observe_nightly_supply.py:259-272 (今日有行即 P1 finding)。"
              "现 n=2 (10-01 ACN / 10-08 PEP)。⚠️ **2026-10-15 审计发现 (待用户裁)**: "
              "commit 80d8817ba (10-09, pipeline 拆分批, message 自述「零行为变更」) 实际携带了 "
              "pipeline.py:3324 `_release_eth_held_exit_for_rth_takeover` — ETH HELD 零成交 intent "
              "在 RTH 开盘被自动归档释放 (release_exit_intent reason=rth_intraday_takeover) + "
              "blocked.discard + continue, 即 **RFC-127 §3.2 阶段1 语义已无开关上线**; "
              "RFC-127 头部至今仍写「阶段1 未实施, 待影子判据+用户二次裁决」, §4 判据从未满足 "
              "(影子面仅 2 行)。副作用: 成功移交时 continue 跳过本影子发射 ⇒ 影子面缩为「移交失败」"
              "子集 (选择偏差), 10-09 后零新增。docs 全域无该行为授权记录。"
              "✅ **2026-10-15 用户裁「追认」** (RFC-127 §7 知情破例留痕): 阶段1 变体语义"
              " (归档释放移交盘中, 零新订单 — 比 §3.2 预注册的 MARKET 补腿更保守) 追认为"
              "现役; §4 原判据面失效留档不再使用; 本影子重新定位=「移交失败」监测面 "
              "(新行=该移交而未移交的残余风险, 出现即 P1)。",
    ),
    ShadowItem(
        id="post_ca4487",
        name="post_ca4487 清洁观测窗 (KR1 换版后, DEB-20261009 §28.11)",
        status=ShadowStatus.OBSERVING,
        description="OKR-2026-10 KR1 退出哈希窗 2026-10-08 换版 (34c7→ca4487) ⇒ 主窗终局 BROKEN 后 "
                    "另立的只读清洁观测面。写方 scripts/daily_invariant_check.py 的 "
                    "config_freeze.post_ca4487 块 (常量 L149-150, 查询 L218-226, 产出 L320-352), "
                    "cron 5 5 * * 2-6。只报告不判分不告警 (not_okr_2026_10_kr1_verdict=true)。",
        data_path="data/invariant_check_latest.json (config_freeze.post_ca4487 块)",
        config_key=None,
        min_n=200,
        count_mode="json_key:config_freeze.post_ca4487.n_closed",
        graduation_criteria="DEB-20261009 §28.11 逐字: 起点 2026-10-09 (10/08 新旧哈希同日交错, "
                            "不作清洁边界); 期望哈希 exit-ca4487172a6f; 目标提示 200 笔 (非判据, "
                            "只作进度); 只读不调 rec()/不参与 status 取劣/不改 oct_cohort; "
                            "出现新哈希只显示 hash_drift。禁止条款: 不得替代或改写 oct_cohort "
                            "BROKEN 终局。",
        eval_script=None,
        eval_on_demand=True,
        no_adjudication_reason="PERMANENT_MEASURE: OKR 功效观测窗, 无可裁决事项 (判决面是 "
                               "oct_cohort BROKEN 终局已定); min_n=200 是进度提示不是判据, "
                               "不得机械迁 READY_TO_EVALUATE",
        created="2026-10-10",
        notes="OKR 关联: docs/OKR-2026-10.md (2026-10-15 已补 KR1 BROKEN 终局回写)。"
              "10-14 收盘读数: n_closed=28/200, single_version, hash 缺失 0 "
              "(data/invariant_check_latest.json)。DEB F13 估达标需 30-40 交易日。"
              "无代码消费方 (OKR-2026-10 面板 okr_2026_10_scoring 未落地, 权威读数=本块+人工)。",
    ),
    ShadowItem(
        id="ext_entry_earnings_criterion_shadow",
        name="财报判据② 新旧口径对照台账 (日历 actual vs period 陈旧, RFC-097)",
        status=ShadowStatus.OBSERVING,
        description="同一批 EXT 候选上「旧判据 (company_earnings surprise>0 + period 龄窗) vs 新判据 "
                    "(当日 Finnhub 日历 epsActual>epsEstimate)」的裁决对照, 逐票-ET日一行, 回答"
                    "「修口径放回多少票」。写方 scripts/extended_hours_entry_executor.py:2201-2245 "
                    "(_record_earnings_criterion_shadow), 开关 EXT_EARNINGS_CRITERION_SHADOW "
                    "(默认 true) 真门控写方 (:133-135, 注册表 config_key 警告的**正例**)。零交易影响。",
        data_path="data/ext_entry_earnings_criterion_shadow.jsonl",
        config_key="EXT_EARNINGS_CRITERION_SHADOW",
        min_n=20,
        count_mode="where:legacy_blocked_new_pass=True",
        graduation_criteria="RFC-097 §预注册判据逐字: ①涨幅带: 自生效日起攒 ≥20 笔延长时段成交 ⇒ "
                            "净均值≥0 维持, <0 或长期不足 ⇒ 删 .env.us 该行回滚; ②对照台账 "
                            "legacy_blocked_new_pass 的日去重计数 ⇒ 10 月财报季裁决「修口径放回多少票」, "
                            "并与 §3.3「温和票 +$23.09/笔」交叉验证; 单票级别不得用比 tick 计数 "
                            "(会被上游闸波动稀释)。",
        eval_script="scripts/ext_earnings_criterion_eval.py",
        eval_on_demand=True,
        no_adjudication_reason="SAMPLE_GATE: EXT 成交自 2026-09-23 仅 5 笔 <20; 台账读数已建, 待 n≥20 再裁; 判据②需人工交叉验证.",
        created="2026-09-22",
        notes="2026-10-15 读数: 5765 行 / 16 ET日 / 2026-09-22~10-14; legacy_blocked_new_pass "
              "True=493。write-only 无代码消费方 (唯一读数=人工 + RFC-097 一次性实证脚本 "
              "verify_earnings_criterion.py)。计数轴注: min_n=20 数的是对照行 "
              "(where:legacy_blocked_new_pass=True), 与判据①的「延长时段成交笔数」不同轴, "
              "判据①须另行按成交口径读。回滚: EXT_EARNINGS_CRITERION_SHADOW=false (无需重启)。"
              "2026-10-18 只读读数: 6674 行 / 18 ET日 / legacy_blocked_new_pass 522 行; "
              "EXT 成交自 2026-09-23 仅 5 笔 (均 +$15.82) ⇒ 判据①样本门未达; "
              "readout scripts/ext_earnings_criterion_eval.py, 待 n≥20 再裁。",
    ),
    # ── 2026-10-17 全链路审计 P0-1: blocked_exits 符号级冻结面的观测 ──
    ShadowItem(
        id="blocked_exit_opportunity",
        name="未结算出场意图冻结期评估机会影子 (审计 P0-1 ④)",
        status=ShadowStatus.OBSERVING,
        description="`_resume_playbook_exits` 把每个未结算 intent 的 (target_env, symbol) 放进 "
                    "blocked_exits, exit_monitor_flow 命中即整行跳过 ⇒ 该标的 SL/TP/时间/因子规则"
                    "全部冻结 (10/1 US.ACN: SL 越线 2.0pp 无保护 6h)。审计 P0-1 处置顺序 = 先影子"
                    "计数 (冻结时长/期间不利位移/SL 本应触发数), 用真实数据定 N(告警升级)/M(受控重评)"
                    "再改行为 ⇒ 本项在 blocked 跳过点按 60s 进程内节流落快照 (写方 "
                    "app/playbook/blocked_exit_opportunity.py, 由 exit_monitor_flow.py 调用), "
                    "零行为变更: 不释放 intent/不改 blocked_exits/不下单/不推送。",
        data_path="data/shadow/blocked_exit_opportunity.jsonl",
        config_key=None,
        min_n=30,
        count_mode="distinct:intent_id",
        graduation_criteria="A1 样本门按 **distinct 冻结 episode** 计 (同一 intent 的 60s 快照 "
                            "合并为 1)。n≥30 后运行 scripts/blocked_exit_opportunity_shadow.py "
                            "(eval_on_demand, 无 cron) 读三轴: ①冻结时长分布 ②期间最大不利位移 "
                            "③按 stop_loss 兜底本应退出的 episode 占比; 读数只用于另立 DEB "
                            "预注册 N/M 阈值与受控重评方案 (行为变更), 不自动改任何出场路径。"
                            "判据未定 N/M 前本项不转正 (纯观测)。",
        eval_script="scripts/blocked_exit_opportunity_shadow.py",
        eval_on_demand=True,
        created="2026-10-17",
        notes="计数锚 = jsonl 的 distinct intent_id (与聚合器 episode 同轴)。聚合产物 "
              "data/shadow/blocked_exit_opportunity_latest.json 由 eval 脚本按需生成, 不作计数锚 "
              "(未跑 eval 时不存在, 挂 data_path 会恒 0)。当前积压 intent=0 (2026-10-17 查询 "
              "playbook_exits.db), 基线从零开始。相关既有告警: pipeline._alert_blocked_exit_age "
              "(30min HIGH / 4h CRITICAL, 每 intent+tier 一次) 与 _alert_close_failure 日去重 — "
              "本影子是定阈证据面, 不是告警面。",
    ),

    # ── 2026-10-22 全链路复查补登批 (R1: 在采未注册 = 军规禁止态, 根因=无反向校验) ──
    ShadowItem(
        id="factor_neg_edge_shadow",
        name="因子负 edge 闸门记分牌 (8/20 复盘 P2, 2026-08-21 用户批准)",
        status=ShadowStatus.OBSERVING,
        description="对 playbook_instructions 已 executed 指令, 按负 edge 闸门谓词 "
                    "(single n≥30 / pair n≥8 且 verdict=negative) 记「若开门会被 SKIP」, "
                    "与 trade_records 实际结局 join。回答: 被拦组净PnL 显著差于放行组 "
                    "⇒ 闸门有判别力可考虑重开 (负edge闸门自 7/11 停用); 否则证伪。 "
                    "写方 scripts/factor_neg_edge_shadow.py (盘后综合复盘 step, Hermes 挂载), "
                    "(symbol,date) 幂等。",
        data_path="data/factor_neg_edge_shadow.jsonl",
        config_key=None,
        min_n=30,
        count_mode="where:would_skip=True",
        graduation_criteria="脚本头部预注册 (2026-08-21, 逐字): 被标组 n≥30 才下结论; "
                            "|Δ胜率|≥10pp 且被标组净PnL<0 记为判别力证据 (门该重开方向); "
                            "否则证伪。每周日复盘面汇报。",
        eval_script="scripts/factor_neg_edge_shadow.py",
        eval_on_demand=True,
        created="2026-08-21",
        notes="2026-10-22 补登记: 在采 2 个月未注册 (10-15 审计 P1-3 根因=无反向校验, "
              "本批为复发实例)。计数轴=would_skip=True 行 (被标组), 与判据「被标组 n≥30」同轴。"
              "回滚: 删本条目 (写方与产物不动)。",
    ),
    ShadowItem(
        id="stop_invalidation_audit",
        name="止损 vs 失效触发器一致性审计 (2026-10-22 指令复查批)",
        status=ShadowStatus.OBSERVING,
        description="record-only 审计: 逐指令比较 raw_instruction_json.invalidation_triggers "
                    "与 stop_loss 的先后关系 (aligned / stop_before / invalidation_before), "
                    "按 (date, instruction_id) 幂等追加。回答: DA 计划的失效位与工程止损 "
                    "是否系统性错位 (错位 ⇒ 止损先于论点失效被打掉)。",
        data_path="data/shadow/stop_invalidation_audit.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="无毕业门 — 纯审计尺子 (对齐率分布监测); 若错位簇集需立修正案 "
                            "另走 RFC。首日读数 (2026-10-22, n=15): 9 aligned / 4 stop_before "
                            "(TSLA/AVGO/UNH/COCO) / 2 invalidation_before (ARM/TSCO)。",
        eval_script="scripts/stop_invalidation_audit.py",
        eval_on_demand=True,
        created="2026-10-22",
        no_adjudication_reason="PERMANENT_MEASURE: 指令卫生审计尺子, 无毕业门 (对齐率分布监测)",
        notes="2026-10-22 当日建当日登 (全链路复查 R1 批: 先登记再攒样, 不再重蹈"
              "「在采未注册」)。回滚: 删本条目 + 删脚本 (产物留档)。",
    ),
]


# ── last_evaluated 从 ADJUDICATED.read_at 派生 (2026-09-26, RFC-109④-B) ──────────
# 为什么需要: 同一个事实("判据最近一次被实测读出结论的日期")此前要**手工写两处** —
# ShadowItem.last_evaluated 与 ADJUDICATED[id]["read_at"]。裁决落档时只写了 ADJUDICATED
# (它是待裁面的真实判据源), 注册项字段漏改 ⇒ 实测 **18/26 条失同步**, 其中 7 条
# last_evaluated 仍是 None。
# 后果不是纯账面: 以 "last_evaluated=None 且 status=READY" 为轴的欠账盘点会把这些
# **已裁决**项报成 "从未评估过的僵尸尺子" (2026-09-26 实锤: 报出 "9 条从未评估",
# 逐条查 ADJUDICATED 后 12/12 全已裁决) ⇒ 假信号会让人重复跑已裁完的评估。
# 修法 = 删根因 (单点派生), 不是手工同步 18 行 (下次落档照样会漏):
#   · 模块加载时按 ADJUDICATED 覆盖 last_evaluated, 该字段从此**派生**, 不需人工维护;
#   · 取 max(两者) 而非直接覆盖: 保护 ADJUDICATED 里**没有**登记的项 (如 da_glm_shadow
#     2026-09-15, tests/test_da_glm_shadow.py 钉住) 与"已评估但尚未落 ADJUDICATED"的项,
#     日期只前进不回退;
#   · 消费方无需改: 该字段此前**零生产代码消费** (实测: summary() 不印它, 待裁面
#     criteria_status 读的是 ADJUDICATED/adjudication_state, 全仓唯一读方是一个测试断言),
#     所以修它零行为变更 — 修的是**人看注册表时的可读性**与欠账盘点的正确性。
def _sync_last_evaluated_from_adjudicated() -> int:
    """用 ADJUDICATED.read_at 补齐/前进各注册项 last_evaluated。返回被更新的条数。"""
    n_updated = 0
    for _s in REGISTRY:
        _adj = ADJUDICATED.get(_s.id)
        if not _adj:
            continue
        _read_at = _adj.get("read_at")
        if not _read_at:
            continue
        # 只前进不回退 (ISO 日期字符串可直接字典序比较)
        if not _s.last_evaluated or str(_read_at) > str(_s.last_evaluated):
            _s.last_evaluated = str(_read_at)
            n_updated += 1
    return n_updated


_sync_last_evaluated_from_adjudicated()


def get_observing() -> list[ShadowItem]:
    return [s for s in REGISTRY if s.status == ShadowStatus.OBSERVING]


def get_ready_to_evaluate() -> list[ShadowItem]:
    return [s for s in REGISTRY if s.status == ShadowStatus.READY_TO_EVALUATE]


def refresh_counts() -> list[str]:
    """从数据文件刷新各 OBSERVING 事项的 current_n, 并自动迁移状态。

    返回状态变迁通知列表 (供推送)。
    """
    import json as _j
    from pathlib import Path as _P

    _base = _P(__file__).resolve().parents[2]
    notifications = []

    for s in REGISTRY:
        if s.status not in (ShadowStatus.OBSERVING, ShadowStatus.READY_TO_EVALUATE):
            continue
        n = _count_samples(s, _base)
        if n is not None:
            s.current_n = n
        if (s.status == ShadowStatus.OBSERVING and not s.no_adjudication_reason
                and s.current_n >= s.min_n):
            s.status = ShadowStatus.READY_TO_EVALUATE
            notifications.append(
                f"🔔 影子 [{s.id}] {s.name} 样本量已达 {s.current_n}/{s.min_n} → "
                f"READY_TO_EVALUATE。⚠️ 仅**样本量**达标, 不代表判据通过 — "
                f"裁决前必须跑 eval_script 实测判据 (READY≠毕业, 2026-09-13 实锤: "
                f"5 个 READY 影子实测判据 0 个过门)。判据: {s.graduation_criteria}")
    return notifications


def _count_samples(s: ShadowItem, base) -> int | None:
    """按事项的 data_path 计算样本量。"""
    import json as _j
    from pathlib import Path as _P

    path_str = s.data_path.split(" (")[0].strip()  # 取第一个路径, 去掉括号说明

    if "trigger_miss_telemetry" in path_str:
        reason = None
        if "reason=" in s.data_path:
            reason = s.data_path.split("reason=")[1].split(",")[0].split(")")[0].strip()
        p = base / "data" / "trigger_miss_telemetry.jsonl"
        if not p.exists():
            return 0
        seen = set()
        for line in open(p, encoding="utf-8"):
            try:
                r = _j.loads(line)
            except Exception:
                _log.warning("jsonl parse failed", exc_info=True)
                continue
            if reason and r.get("reason") != reason:
                continue
            seen.add((r.get("inst_id"), r.get("date")))
        return len(seen)

    p = base / path_str
    if not p.exists():
        return None
    if path_str.endswith(".jsonl"):
        try:
            if s.count_mode.startswith("distinct:"):
                fld = s.count_mode.split(":", 1)[1]
                seen = set()
                for _line in open(p, encoding="utf-8"):
                    if not _line.strip():
                        continue
                    try:
                        seen.add(_j.loads(_line).get(fld))
                    except Exception:
                        _log.warning("jsonl parse failed", exc_info=True)
                        continue
                seen.discard(None)
                return len(seen)
            if s.count_mode.startswith("distinct_where:"):
                # "distinct_where:field=value" — 在满足字段条件的行里计**去重后**的样本数。
                # 2026-09-18 short_chase_ban_shadow 引入: 命中行逐轮写 (同一指令多行) ⇒
                # 判据的 n 轴是「指令数」而非「行数」, 而 where: 只能计数行 ⇒
                # 需与 distinct: 组合。参数 = field=value, 去重键另由 distinct_key 隐含为
                # inst_id (无则退化为 decision_id/ts)。
                _rest = s.count_mode.split(":", 1)[1]
                fld, _, val = _rest.partition("=")
                _seen = set()
                for _line in open(p, encoding="utf-8"):
                    if not _line.strip():
                        continue
                    try:
                        _r = _j.loads(_line)
                    except Exception:
                        _log.warning("jsonl parse failed", exc_info=True)
                        continue
                    if str(_r.get(fld)) != val:
                        continue
                    _seen.add(_r.get("inst_id") or _r.get("decision_id") or _r.get("ts"))
                _seen.discard(None)
                return len(_seen)
            if s.count_mode.startswith("where_nonempty:"):
                # "where_nonempty:field" — 计该字段为真值 (非空 list/str/非零) 的行。
                # 2026-09-11 premarket_invalidation 登记引入: flagged 口径 = would_invalidate
                # 非空行, where: 的 field=value 字符串相等语义无法表达 list 非空。
                fld = s.count_mode.split(":", 1)[1]
                cnt = 0
                for _line in open(p, encoding="utf-8"):
                    if not _line.strip():
                        continue
                    try:
                        _hit = bool(_j.loads(_line).get(fld))
                    except Exception:
                        _hit = False  # 坏行不计分母 (同 where: R5 纪律)
                    if _hit:
                        cnt += 1
                return cnt
            if s.count_mode.startswith("where:"):
                # "where:field=value" — 仅计满足字段条件的行 (2026-09-01 复查收口:
                # da_glm_shadow live-only 口径落地, 原 lines 把 test 行也计入)
                fld, _, val = s.count_mode.split(":", 1)[1].partition("=")
                cnt = 0
                for _line in open(p, encoding="utf-8"):
                    if not _line.strip():
                        continue
                    try:
                        _r = _j.loads(_line)
                        _hit = str(_r.get(fld)) == val
                    except Exception:
                        _hit = False  # R5: 解析失败行不计 where 分母 (赋值体不计棘轮)
                    if _hit:
                        cnt += 1
                return cnt
            return sum(1 for _ in open(p, encoding="utf-8") if _.strip())
        except Exception:
            return None
    if path_str.endswith(".json"):
        try:
            d = _j.loads(p.read_text(encoding="utf-8"))
            if s.count_mode.startswith("json_key:"):
                # "json_key:<key>" — 读 .json 产物里**指定键**的整数当计数锚
                # (2026-09-26 B21/B22 登记引入)。为什么需要: 下面四个固定键只能取一个,
                # 而同一 report 需服务两条**不同 n 轴**的判据 (B21=执行类已结算行数,
                # B22=执行类 ∧ quote_age 非空行数), 固定键无法同轴表达两条 ⇒ 必须显式指定。
                # 2026-09-26 扩展: 支持**点号嵌套路径** ("a.b.c") — rfc104_eth_event_plan 的
                # n 轴是判据产物里 criteria.trading_days.actual (交易日覆盖数), 顶层无 n 键。
                # 同 gap_chase_gate 教训: 键不存在时返回 None (保持原值), 不返回 0
                # 伪装"断供", 也不 fallback 到别的键 (会静默换轴)。
                _k = s.count_mode.split(":", 1)[1].strip()
                _cur: object = d
                for _part in _k.split("."):
                    if not isinstance(_cur, dict) or _part not in _cur:
                        return None
                    _cur = _cur[_part]
                if isinstance(_cur, bool):          # bool 是 int 子类, 必须排除
                    return None
                return int(_cur) if isinstance(_cur, int) else None
            if isinstance(d, dict):
                for k in ("n", "n_total", "n_events", "stats"):
                    if k in d:
                        v = d[k]
                        if isinstance(v, int):
                            return v
                        if isinstance(v, dict):
                            return sum(vv.get("n", 0) for vv in v.values()
                                       if isinstance(vv, dict))
            return None
        except Exception:
            return None
    return None


def summary() -> str:
    lines = ["=== 影子观测注册表 ==="]
    by_status = {}
    for s in REGISTRY:
        by_status.setdefault(s.status.value, []).append(s)
    for st in ("OBSERVING", "READY_TO_EVALUATE", "GRADUATED", "KILLED", "SUPERSEDED"):
        items = by_status.get(st, [])
        if not items:
            continue
        lines.append(f"\n[{st}] ({len(items)})")
        for s in items:
            progress = f"n={s.current_n}/{s.min_n}" if s.min_n else ""
            lines.append(f"  {s.id:25} {s.name:20} {progress:12} {s.notes[:60]}")
    return "\n".join(lines)


# ── 挂载核对哨兵 (2026-09-01 全链体检后用户裁"按建议执行" 建议①) ──
# 两例实锤驱动: gap_chase_gate (B1, 8/29注册) 与 ssr_shadow_daily (RFC-062④, 8/2落地)
# 均"已登记/已注册但从未挂载", 观察悬空至体检才发现。哨兵每周机械核对:
#   ① eval 挂载: eval_script 是否在 cron 解析面 / scheduler / ops / 采集脚本引用中
#   ② 数据产出: data_path 文件是否超龄 (14天无更新) 或缺失 (非初生期)
# 2026-09-01 复查收口收紧两处盲区 (复查实锤: da_glm_shadow.py:165 的注释提及掩盖
# eval 悬空; pipeline.py:8538 的 docstring 提及掩盖判罪线无挂载):
#   ③ 引用判定收紧: 源码引用须落在代码位 (字符串字面量/调用) — # 注释与行首
#      docstring 提及不算挂载 (_code_only tokenize 剥离)
#   ④ cron 解析面扩至 /etc/crontab + /etc/cron.d 全部文件 (含点号名, 401bedcb
#      教训), 且注释行不计挂载
_STALE_DAYS = 14      # 产出超龄阈值 (日更影子两周无产出 = 疑似断流)
_GRACE_DAYS = 10      # 初生豁免: 新注册事项等首夜产出, 不报文件缺失

# ── check_artifact_consumers 口径常量 (2026-09-26, 坑2「哨兵单向」守卫) ──────────
_TERMINAL_STATUS = (ShadowStatus.KILLED, ShadowStatus.SUPERSEDED, ShadowStatus.GRADUATED)
# 展示面: 只读产物渲染看板/词表, 不喂生产决策 ⇒ 不算「生产消费者」(否则每条影子都命中)
_DISPLAY_ONLY_SOURCES = ("shadow_board.py", "shadow_lexicon.py")
# 已显式认领的「终态但产物仍喂生产链」— **必须写理由**, 否则守卫失去意义。
# 刻意用模块级常量而非 notes 自由文本: 本文件 collection_retired 注释已实证
# 「notes 里的自由文本警告没拦住 ⇒ 规则必须放字段级」(2026-09-26 RFC-109⑤-B)。
_CONSUMER_ACK: dict[str, str] = {
    "universe_expansion_shadow": (
        "2026-09-26 用户裁「退役」时实测确认: 只摘 score+eval cron, **capture cron "
        "`30 12 * * 1-5` 必须保留** — llm_universe_scan.py `_gather_market_corroboration` "
        "的 fallback 只读 type=='capture' 行, 且 DEB-20260914 实测「da_feed 窗 ET 07:40 "
        "结构上永远早于生产者 DA ET 08:00 ⇒ 次次落到 capture 回退」+ 盘中脉搏在生产两窗 "
        "结构上永不成立 ⇒ capture 是 UE 生产 prompt「市场佐证」段的**常态原料**; "
        "crontab 有 inject ∧ entry_enabled=true ⇒ 摘 capture = 改 UE 交易行为, 超授权。"
        "详见本条 verdict_history 同日 KILLED 记录 evidence ②。"),
    # ── 存量棘轮 (2026-09-26 守卫首跑即命中的 3 条, 逐条实测认领, 非批量豁免) ──
    # 判据: 消费者脚本是**终点报表** — 其产物无其他脚本读 (grep 全仓仅自身), 且只 print/push
    #       飞书 ⇒ 停采只会让报表数字停增, **不会断生产料**, 与 universe_expansion_shadow
    #       (喂 LLM prompt ⇒ 改交易行为) 性质不同。故降级为已认领, 不占周检噪音预算。
    "limit_entry": (
        "消费者 scripts/limit_entry_shadow_replay.py (cron 35 5 * * 2-6) 是**终点回放报表**: "
        "产物 OUT_SUMMARY/明细 jsonl 全仓无其他脚本读 (grep 实测), 只 print。"
        "⇒ 停采只让回放数字停增, 不断生产料。"),
    "da_glm_shadow": (
        "消费者 scripts/daily_llm_token_report.py (cron 20 6 * * 2-6 --push) 是**终点 token 成本报表**: "
        "读该影子只为统计 glm_tokens_in/out 累进成本表, 产物 push 飞书, 无脚本读其输出。"
        "⇒ 停采只让成本表少一行, 不断生产料。"),
    "da_prompt_distill_shadow": (
        "同上 daily_llm_token_report.py 终点报表 (读 v2_tokens_in/out 统计蒸馏增量成本)。"
        "⇒ 停采只让成本表少一行, 不断生产料。"),
    # 2026-10-15 (审计 P1-3 结案附带): 冻结归档的展示型消费认领
    "ue_short_boost_shadow": (
        "2026-10-15 终裁 KILLED 后实测: 消费者 = scripts/shadow_board.py (段13③ 只读展示) "
        "+ scripts/ue_short_boost_final_settlement.py (一次性结算器, 留档可读); 写方链 "
        "(UE 直写 inject) 已于 10-06 退役 ⇒ 产物为冻结归档, 读它不改任何生产行为 "
        "⇒ 归档展示型消费, 认领不占周检噪音预算。"),
    # ── 2026-10-03 清理批新增认领 (终态但产物仍喂生产链, 逐条写理由) ──────────────
    "premarket_peer_shadow": (
        "2026-10-03 清理批判 KILLED 时实测确认: **采集必须保留** — "
        "scripts/llm_universe_scan.py `_gather_peer_surge` (cron 主 scan 21:10 在挂) 只读本文件"
        "当日 peers_snapshot 行, 是 UE 盘前 clue (evidence_ref=peer_surge) 的唯一数据源; "
        "摘采集 cron = 改 UE 交易行为, 超出判据退役授权 — 与 universe_expansion_shadow "
        "9/26 先例同型 (判据 KILLED ≠ 采集退役)。详见本条 verdict_history。"),
    "ue_midday_shadow": (
        "2026-10-03 判据 FAIL 退役: llm_universe_scan.py 是**写方** (scan --midday 分支, "
        "唯一引用=模块常量 _MIDDAY_PATH), 非生产读方。退役后午间 cron 已注释 + "
        "midday_shadow_enabled=false ⇒ 该分支 inert (不再被调度; 即使手动跑也被开关跳过)。"
        "eval 脚本仅按需读历史留档。停采不断任何生产料。"),
    "ue_earnings_clue_shadow": (
        "2026-10-03 write-only 退役 (grep 实测): llm_universe_scan.py 是**写方** (clue 收集分支, "
        "唯一引用=模块常量 _EARNINGS_SHADOW_PATH), 非生产读方。clue_earnings_shadow=false "
        "⇒ 该分支 inert; 全仓无其他读方。停采不断任何生产料。"),
    "eth_ext_entry_prefilter": (
        "2026-10-03 SUPERSEDED 归档: 活跃 cron 调 extended_hours_entry_executor.py, 但该文件"
        "对本 jsonl 是**写方** (PREFILTER_SHADOW 常量 + _record_prefilter_shadow); 现行 cron "
        "未传 EXT_PREFILTER_SHADOW 且默认 false ⇒ 写分支 inert, grep 实测无生产读方。"
        "停采不断任何生产料。"),
    "p1_1_vwap_slope": (
        "2026-10-18 减法批补登记: 消费者 scripts/gate_block_counterfactual.py (cron 50 6 * * 6) "
        "是**终点反事实报表** — 只读 trigger_miss_telemetry.jsonl 的 "
        "reason=falsification_vwap_slope 行, 产物 gate_block_counterfactual_latest.json "
        "全仓无其他脚本读, 只落盘/print, 无 push/生产回灌。⇒ 停采只会让周报数字停增, "
        "不断生产料; p1_1 门本体已 GRADUATED, 采集继续。"),
    "ext_entry_exec_price_shadow": (
        "2026-10-19 退役后实测: 活跃 cron 内的 extended_hours_entry_executor.py 是**写方** "
        "(只在 EXT_ENTRY_EXEC_PRICE_SHADOW=true 分支写该 jsonl); crontab 两行已改 false "
        "⇒ 写分支 inert, grep 全仓无生产读方; 停采不断任何生产料。数据保留。"),
    "ue_tier_gate": (
        "2026-10-19 退役后实测: 活跃 cron 的 llm_universe_scan.py 是**写方** "
        "(只在 inject 路径调用 _log_tier_gate_shadow); 现行 scan --da-feed 明确 skip inject "
        "⇒ 写分支 inert; eval cron 与 data_freshness_watchdog 条目已摘; grep 全仓无生产读方; "
        "停采不断任何生产料。数据保留。"),
}


def _code_only(text: str) -> str:
    """挂载核对用: 剥离 # 注释与行首 docstring 后的代码文本 (token 级)。

    真引用 (cron 参数 / subprocess 路径参数 / runner 常量) 都在字符串字面量里;
    注释与 docstring 提及不算挂载。tokenize 失败退回原文 (宁可多算挂载, 不漏报
    真引用)。token 无分隔拼接: 点分文件名 (a.b.py) 在 token 流中被拆散,
    拼回才能匹配 basename。
    """
    import io
    import tokenize as _tk
    kept: list[str] = []
    code_on_line = False
    try:
        for tok in _tk.generate_tokens(io.StringIO(text).readline):
            if tok.type == _tk.COMMENT:
                continue
            if tok.type == _tk.NEWLINE:  # 逻辑行界; NL(括号内换行)不重置 — 跨行调用参数仍算代码
                code_on_line = False
                continue
            if tok.type == _tk.STRING and not code_on_line:
                continue  # 行首独立字符串 = docstring
            if tok.type in (_tk.NL, _tk.INDENT, _tk.DEDENT, _tk.ENDMARKER):
                continue
            code_on_line = True
            kept.append(tok.string)
    except Exception:
        return text
    return "".join(kept)


def _cron_surface_texts() -> list[str]:
    """cron 全解析面原始文本: 用户 crontab + /etc/crontab + /etc/cron.d 全部文件 (含点号名)。

    401bedcb 教训: Alinux8 cronie 不忽略含点文件名, .bak 克隆同样被执行 —
    核对面必须覆盖目录内全部文件, 不能只看 crontab -l。注释行的剥离由调用方做。
    """
    import subprocess as _sp
    parts: list[str] = []
    try:
        parts.append(_sp.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=10).stdout or "")
    except Exception:
        parts.append("")
    etc_files = [_ETC_CRONTAB] + (
        sorted(_ETC_CRON_D.iterdir()) if _ETC_CRON_D.is_dir() else [])
    for cp in etc_files:
        if not cp.is_file():
            continue
        try:
            parts.append(cp.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:
            # R5 留痕: cron 面文件不可读必须显性 (可能漏判挂载)
            print(f"[mount-sentinel] cron 面文件读取失败跳过: {cp}: {e}")
    return parts


def _source_files() -> dict:
    """引用源 (scheduler/ops/采集脚本) 的 {路径: 源码原文}, 只读。"""
    base = Path(__file__).resolve().parents[2]
    sources = {}
    cand = [base / "app" / "core" / "scheduler_core.py"]
    cand += list((base / "ops" / "hermes").glob("*.py"))
    cand += list((base / "scripts").glob("*.py"))
    for pth in cand:
        try:
            _text = pth.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            _text = None  # R5 留痕: 不可读源文件不计引用源 (权限/编码异常, 罕见)
            print(f"[mount-sentinel] 源文件读取失败跳过: {pth}: {e}")
        if _text is not None:
            sources[str(pth)] = _text
    return sources


def _mounted_sources() -> tuple[str, dict]:
    """返回 (cron 解析面文本, {路径: 源码} 引用源字典), 只读。"""
    return "\n".join(_cron_surface_texts()), _source_files()


def check_mounting() -> list[str]:
    """挂载核对: 返回告警行列表 (供周检推送), 空列表=全部在挂。

    判定口径 (2026-09-01 复查收口): cron 面取非注释行; 源码引用取 _code_only
    剥离注释/docstring 后的代码位 — 纯注释/docstring 提及不算挂载。
    """
    from datetime import date as _date, datetime as _dt
    base = Path(__file__).resolve().parents[2]
    cron_text, sources = _mounted_sources()
    cron_active = "\n".join(
        l for l in cron_text.splitlines() if not l.lstrip().startswith("#"))
    today = _date.today()
    warnings = []

    for s in REGISTRY:
        if s.status not in (ShadowStatus.OBSERVING, ShadowStatus.READY_TO_EVALUATE):
            continue
        created_recent = False
        try:
            created_recent = (today - _dt.strptime(s.created, "%Y-%m-%d").date()).days < _GRACE_DAYS
        except Exception:
            created_recent = False  # 保守: 日期不可解析不给初生豁免 (宁可多报不漏报)

        # ① eval 挂载核对
        if s.eval_script and not s.eval_on_demand:
            bn = s.eval_script.split("/")[-1]
            mounted = bn in cron_active
            if not mounted:
                for path, text in sources.items():
                    if path.endswith(("shadow_registry.py", bn)):
                        continue  # 注册表自身引用与脚本自身不算挂载
                    if bn in _code_only(text):
                        mounted = True
                        break
            if not mounted:
                warnings.append(
                    f"🪝 悬空观察 [{s.id}] {s.name}: eval_script={s.eval_script} "
                    f"不在 cron解析面/scheduler/ops/采集引用中 (B1 与 ssr_shadow_daily 同型病灶, "
                    f"请补挂或改判 eval_on_demand/状态)")

        # ② 数据产出核对 (DB 内口径跳过 — 由各自 eval 现算)
        p = base / s.data_path.split(" (")[0].strip()
        if str(p).endswith(".db"):
            continue
        if not p.exists():
            if not created_recent:
                warnings.append(
                    f"🪝 产出缺失 [{s.id}] {s.name}: data_path={s.data_path} 不存在 "
                    f"(创建于 {s.created}, 已过初生豁免期 {_GRACE_DAYS} 天)")
            continue
        try:
            _mtime = p.stat().st_mtime
        except Exception as e:
            print(f"[mount-sentinel] stat 失败跳过超龄判定: {p}: {e}")
            continue  # 注: 本行在 except 多行体内, 不计棘轮
        age = (today - _dt.fromtimestamp(_mtime).date()).days
        if age > _STALE_DAYS and not s.stale_expected:
            warnings.append(
                f"🪝 产出超龄 [{s.id}] {s.name}: {p.name} 已 {age} 天未更新 "
                f"(阈值 {_STALE_DAYS} 天, 疑似采集断流)")
    return warnings


def check_artifact_consumers() -> list[str]:
    """🔵 反向挂载哨兵: 终态影子的产物仍有**cron 活跃消费者** = 停采/摘 cron 会断生产料.

    立起理由 (2026-09-26 实测, 我自己差点踩): `check_mounting` 是**单向**的 —
    只查「eval_script 存在但没挂 cron ⇒ 悬空」, **不查**「你要摘的那条 cron 有没有生产读方」。
    实例: `universe_expansion_shadow` 判据 FAIL 用户裁退役, 我照判据原文「删 cron + 开关置 false,
    零残留」准备摘 capture cron `30 12 * * 1-5` —— 但 `llm_universe_scan.py`
    `_gather_market_corroboration` 的 fallback **只读** `type=='capture'` 行, 且 DEB-20260914 已实测
    「UE 的 da_feed 窗 (ET 07:40) 结构上永远早于生产者 DA (ET 08:00) ⇒ 该窗**次次落到 capture 回退**」,
    而 crontab 有 `llm_universe_scan.py inject` ∧ `entry_enabled: true` ⇒ UE 真下单
    ⇒ 摘 capture = **改 UE 交易行为**, 远超「判据退役」授权。靠人工 grep 才发现, 本该是哨兵的活。
    这正是本文件 `collection_retired` 字段注释预警的形态: 「判据退役 (KILLED/FAIL) **不等于**
    没有消费者 — 实锤 capital_flow 的原料在喂生产门」。

    判定口径 (刻意收窄, 防噪音风暴 — 原型全量命中 36 条/22 条 cron 活跃, 不可接受):
      ① 只报**终态** (KILLED/SUPERSEDED/GRADUATED): 现役项有消费者是正常的 (影子本该有读方);
      ② 消费者必须在 **cron 活跃面** (非注释行): 仅静态引用 = 报表/研究脚本读历史数据, 不断料;
      ③ 排除: 写者 (与产物同名的脚本) / 该项自己的 eval_script / 展示面 (shadow_board,
         shadow_lexicon) / 注册表自身 — 否则 4 条里 3 条是假阳;
      ④ 豁免走**模块级常量** `_CONSUMER_ACK` (不走 notes 自由文本: 本文件
         `collection_retired` 注释已实证「notes 里的自由文本警告没拦住 ⇒ 规则必须放字段级」)。
    口径复用 `check_mounting` 的既有件: `_code_only` (剥注释/docstring, 纯注释提及不算引用)
    + `_mounted_sources` (crontab + /etc/crontab + /etc/cron.d 全解析面, 401bedcb 教训)。
    性能: 原型对每文件跑 tokenize = 111s (周检 subprocess timeout=90s ⇒ 会超时, 守卫自己变死门);
    改为「廉价 `bn in text` 粗筛 → 只对命中文件跑 _code_only」= **1.09s** (提速 102×, 结果一致)。
    """
    cron_text, sources = _mounted_sources()
    cron_active = "\n".join(
        l for l in cron_text.splitlines() if not l.lstrip().startswith("#"))
    warnings = []
    for s in REGISTRY:
        if s.status not in _TERMINAL_STATUS:
            continue                      # ① 只管终态 (判据退役 ≠ 采集退役 的防呆面)
        if s.id in _CONSUMER_ACK:
            continue                      # ④ 已显式认领 (理由写在该常量旁)
        bn = s.data_path.split(" (")[0].strip().split("/")[-1]
        if not bn or bn.endswith(".db"):
            continue                      # DB 内口径由各自 eval 现算, 无产物文件可断供
        stem = bn[:-6] if bn.endswith(".jsonl") else Path(bn).stem
        own_eval = (s.eval_script or "").split("/")[-1]
        consumers = []
        for path, text in sources.items():
            pbn = Path(path).name
            if pbn == own_eval or pbn in _DISPLAY_ONLY_SOURCES or pbn == "shadow_registry.py":
                continue                  # ③ 自身 eval / 展示面 / 注册表
            if Path(pbn).stem == stem:
                continue                  # ③ 写者 (与产物同名脚本) 不算消费者
            if bn not in text:
                continue                  # 廉价粗筛 (省 tokenize, 见性能注)
            if bn not in _code_only(text):
                continue                  # 精筛: 纯注释/docstring 提及不算
            if pbn in cron_active:
                consumers.append(pbn)     # ② 只认 cron 活跃面的消费者
        if consumers:
            warnings.append(
                f"🔗 终态产物仍有生产消费者 [{s.id}] {s.name} (status={s.status.value}): "
                f"{s.data_path} 被 cron 活跃脚本读 → {', '.join(sorted(consumers))}。"
                f"⇒ **判据退役 ≠ 采集退役**: 摘该产物的采集 cron 会断这些脚本的料; "
                f"若确属合法保留 (产物是生产链原料), 在 _CONSUMER_ACK 显式认领并写明理由, "
                f"否则先拆依赖再停采 (先例: universe_expansion_shadow 的 capture 喂 "
                f"llm_universe_scan 佐证段 fallback, DEB-20260914 实测为常态路径)。")
    return warnings


def check_intent_uniqueness() -> list[str]:
    """意图唯一性守卫 (2026-09-11, DEB-20260911 P0-3①, 用户裁「按建议执行」).

    立起理由: A_short_chase (09:35 快照口径) 与 short_chase_ban_shadow (触发时刻口径)
    同意图「逆势追空禁入」同阈值 +3%, 独立攒样独立裁决、台账零互引, 且已在真实数据给出
    矛盾读数 (BIDU 9/8 同指令 7 秒内 −7.65% vs +4.07%, 连 prev_close 分母都不同) ⇒
    「一个 FAIL 一个 PASS → 双转正打架」不是假想风险。现有守卫 (test_control_loop_ledger /
    check_mounting / D1 watchdog) 只强制**存在性登记**, 本守卫补**意图唯一性**层。

    判定口径:
      ① 同一非空 intent 的现役事项 (OBSERVING/READY_TO_EVALUATE) ≥2 时, 每项的 cross_ref
         必须互指其余项 id — 缺指 = 告警 (裁决前须先裁归属/归并, 防双转正);
      ② cross_ref 引用的 id 必须存在于 REGISTRY (任意状态, 含 KILLED/SUPERSEDED 留痕项),
         悬空引用 = 告警;
      ③ 互引必须双向: 目标若也现役却不回指 = 告警。补①的盲区 — 本对实际意图标签不同
         (long_knife_gate vs short_chase_ban, A 退役后假设归并而非同标签), ①不触发,
         单边引用只在一条周检里可见;
      ④ REGISTRY 内 id 必须唯一: 重复 id 会让 by_id 静默吞掉一条、summary 重复出两行
         (2026-09-11 补登记时实际发生过一次), 且守卫自身读不到被吞的那条;
      ⑤ 守卫自身异常必须显性产出 *_detector_error (元环规则 5: 「零违反」与「守卫崩了」
         必须可区分, 严禁 except:pass)。
    返回告警行列表 (供周检推送), 空列表 = 全部通过。
    """
    warnings = []
    try:
        live_statuses = (ShadowStatus.OBSERVING, ShadowStatus.READY_TO_EVALUATE)
        by_id: dict = {}
        for s in REGISTRY:
            if s.id in by_id:
                warnings.append(
                    f"🧭 注册表 id 重复 [{s.id}]: 同名条目 {len([x for x in REGISTRY if x.id == s.id])} "
                    f"条 — 互引/计数会静默只认一条, summary 会重复出行, 必须合并或改名")
            by_id.setdefault(s.id, s)
        known = set(by_id)
        by_intent: dict = {}
        for s in REGISTRY:
            if s.status in live_statuses and s.intent:
                by_intent.setdefault(s.intent, []).append(s)
        for intent, items in sorted(by_intent.items()):
            if len(items) < 2:
                continue
            for s in items:
                peers = {o.id for o in items if o.id != s.id}
                missing = peers - set(s.cross_ref or ())
                if missing:
                    warnings.append(
                        f"🧭 意图不唯一 [{s.id}] {s.name}: 与 {sorted(missing)} 共享意图 "
                        f"'{intent}' 但 cross_ref 未互指 — 同意图双影子独立裁决有双转正风险, "
                        f"裁决日前须先裁归属/归并 (DEB-20260911 P0-3①)")
        for s in REGISTRY:
            for ref in (s.cross_ref or ()):
                if ref not in known:
                    warnings.append(
                        f"🧭 悬空互引 [{s.id}]: cross_ref '{ref}' 不在注册表 — "
                        f"互引必须指向已登记事项 (含终态留痕项)")
                    continue
                t = by_id[ref]
                if t.status in live_statuses and s.id not in (t.cross_ref or ()):
                    warnings.append(
                        f"🧭 单向互引 [{s.id}] → [{ref}]: 目标现役却未回指 — 同意图/同假设的"
                        f"两条必须在彼此周检里都可见 (指向终态留痕项不要求回指)")
    except Exception as e:
        warnings.append(
            f"🧭 intent_uniqueness_detector_error: 守卫自身异常 (≠零违反, 元环规则5): {e}")
    return warnings
