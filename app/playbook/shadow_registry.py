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
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


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
    config_key: Optional[str] = None
    min_n: int = 30
    current_n: int = 0
    graduation_criteria: str = ""
    eval_script: Optional[str] = None
    created: str = ""
    last_evaluated: Optional[str] = None
    verdict_history: list = field(default_factory=list)
    notes: str = ""
    # 计数口径: "lines" (默认, jsonl 行数) | "distinct:<field>" (按字段去重计数,
    # 用于首日回补大量历史数据的事项 — 回补行只算 1 个样本, 防立即超 min_n
    # 触发永久 READY 通知风暴; 2026-08-29 futu_channel_iv_hv 落地时引入)
    count_mode: str = "lines"


REGISTRY: list[ShadowItem] = [
    # ── Phase 1 (2026-08-23) ──
    ShadowItem(
        id="p1_1_vwap_slope",
        name="VWAP 斜率证伪门",
        status=ShadowStatus.OBSERVING,
        description="入场前 5min 量加权价逆方向 → 动量反驳。reentry+UE 启用拦截, DA 只记不拦(对比组)。",
        data_path="data/trigger_miss_telemetry.jsonl (reason=falsification_vwap_slope)",
        config_key="playbook.execution_parameters.falsification_gate_vwap_slope_enabled",
        min_n=30,
        graduation_criteria="reentry+UE 组 n≥30 + 日聚类 t≤-2 + DA 对比组方向一致 → 翻 da_enforce=true",
        eval_script="scripts/falsification_gate_weekly_review.py",
        created="2026-08-23",
        notes="按源分流: reentry+UE 拦截, DA 对比组记录不拦; E2 n=136 方向极强",
    ),
    ShadowItem(
        id="p1_3_vol_p50",
        name="量能门 p50 曲线",
        status=ShadowStatus.OBSERVING,
        description="chase 路径分母改历史同分钟 p50。reentry+UE 启用, DA 走线性(对比组)。",
        data_path="data/vol_gate_da_p50_shadow.jsonl",
        config_key="playbook.execution_parameters.volume_gate_vwap_relative",
        min_n=30,
        graduation_criteria="reentry+UE 组 n≥30 chase + 重标定阈值 + DA 对比组方向一致 → 翻 da_enforce=true",
        eval_script="scripts/phase1_gate_backtest.py",
        created="2026-08-23",
        notes="按源分流: reentry+UE 走 p50, DA 走线性; 核心桶 [0.6,0.8) WR 20% 方向强",
    ),
    ShadowItem(
        id="p1_1_capital_flow",
        name="资金流背离 record-only",
        status=ShadowStatus.OBSERVING,
        description="大单净流逆方向 → 背离。回放四关门全不过(p=0.85), 纯 record-only。",
        data_path="data/falsification_gate_shadow.jsonl",
        config_key="playbook.execution_parameters.falsification_gate_capital_flow_shadow",
        min_n=30,
        graduation_criteria="同四关门判据 (capital_flow_replay_verdict.json): inflow/outflow t≤-2 + IS-OOS 同号 + placebo p<0.05",
        eval_script=None,
        created="2026-08-23",
        notes="capital_flow_replay_verdict: 四关全不过, 用户裁决不得拦单",
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
        verdict_history=[
            {"date": "2026-08-24", "verdict": "WEAK_EVIDENCE",
             "evidence": "ic=0.1188 t=1.49 n_pairs=157 oos同号(0.194/0.086) 未显著"},
            {"date": "2026-08-29", "verdict": "FAIL_INTERIM",
             "evidence": "ic=0.0511 t=0.595 n_pairs=137 较8/24衰减; OOS双窗异号(+0.134/-0.051) "
                         "→ 毕业判据FAIL; 未达min_n=540继续攒样, 但若下次IC继续衰减/OOS仍异号 → 建议提前裁KILLED"}],
        notes="行数口径: ~18标的×30交易日≈540行 (当前258); 分钟级原料 data/futu_capital_flow.jsonl; "
              "OFI基线已就位(7817行, overlap 91对, flow_ofi_rank_corr=0.089 低共线)",
    ),
    ShadowItem(
        id="futu_channel_deriv_unusual",
        name="富途期权大单异动 (领先假说)",
        status=ShadowStatus.KILLED,
        description="通道 A: get_derivative_unusual 期权大单事件 → 事件后窗口超额收益配对基线 "
                    "(Cremers-Weinbaum 知情资金先进期权假说)。8/12 已上线, 8/29 补登记。"
                    "8/24 初评: 超额均收益 -0.006%, 超额胜率 47.5% — 暂无领先证据。",
        data_path="data/futu_deriv_unusual.jsonl",
        config_key="futu_channels.deriv_collect_enabled",
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
        notes="8/29 用户裁决 KILLED, 停采生效; 回滚: deriv_collect_enabled 置 true",
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
        notes="count_mode=distinct:ts_et — 按快照时刻去重计数 (每轮采集多标的共享同一 ts); "
              "8/21-8/28 已攒 428 快照 / 7817 行",
        count_mode="distinct:ts_et",
    ),
    ShadowItem(
        id="futu_channel_top_movers",
        name="富途全市场盘中异动榜 (通道 E)",
        status=ShadowStatus.OBSERVING,
        description="通道 E (8/29 新增, 能力利用率审计用户裁'全做'): get_top_movers_rank(Market.US) "
                    "全市场实时异动榜快照 (~1832标的, 每轮1行含Top100涨幅/换手/量比)。"
                    "此前系统盘中无异动发现通道 — 影子先行攒样评估 "
                    "'盘中异动 → 后续窗口收益' 有无信号, 不进门禁。",
        data_path="data/futu_top_movers_shadow.jsonl",
        config_key="futu_channels.movers_shadow_enabled",
        min_n=540,
        graduation_criteria="预注册: 异动榜Top100入选 → 后续30min/日内窗口超额收益配对基线, "
                            "IC 显著才评估接候选池发现通道; 评估脚本待建 (先攒样)。",
        eval_script=None,
        created="2026-08-29",
        notes="盘中每轮(300s节流)1行快照, ~78行/交易日; min_n=540≈7个交易日; "
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
        data_path="data/futu_iv_hv_shadow.jsonl",
        config_key="futu_channels.iv_collect_enabled",
        min_n=30,
        graduation_criteria="残存效应分析 (基线纪律: 在已落地 atr_pct 分桶上算残存, 不另起炉灶): "
                            "IV/HV 于交易日 join trade_records, 桶内增量显著才毕业。"
                            "首日回补 250 日历史 → 历史残存效应立即可跑。",
        eval_script=None,
        created="2026-08-29",
        notes="count_mode=distinct:collected_at — 首日回补 250 日历史 (~5000 行) 只算 1 个攒样日, "
              "回补行保留供历史残存效应分析; min_n=30 攒样日",
        count_mode="distinct:collected_at",
    ),

    # ── 既有影子 (Phase 0 / 7月) — 代表性事项, 非穷举 ──
    ShadowItem(
        id="entry_band_widen",
        name="入场带 ATR 宽化",
        status=ShadowStatus.OBSERVING,
        description="RFC-065: 入场带按 ATR 只放宽不收窄 (chase 上限 8%)。record-only 常开攒样。",
        data_path="data/entry_band_widen_shadow.jsonl",
        config_key="playbook.execution_parameters.entry_band_widen_shadow_enabled",
        min_n=60,
        graduation_criteria="预注册见 RFC-065: sign-flip B=10000 单侧α=0.025 + 双条件 + 符号共门",
        eval_script="scripts/limit_entry_shadow_replay.py",
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
        graduation_criteria="双臂净差 t-test + 按日聚类 MDE",
        eval_script="scripts/eval_vwap_gate.py",
        created="2026-08-07",
    ),
    ShadowItem(
        id="gap_chase_gate",
        name="追涨极端缺口门 (aligned_gap≥5%)",
        status=ShadowStatus.OBSERVING,
        description="2026-08-29 用户裁决注册 (MFE漏斗审计①队列): 顺向 aligned_gap≥5% 追缺口入场的"
                    "反事实拦截影子。回溯基线(7/1起): n=10, 笔均PnL -$28.3 (全场最差档); 导火索=GAP 8/28 "
                    "+18.95% gap 追入(thesis自注'严禁追涨'仍成交), MFE+5.9%→当日-3.6%过夜。"
                    "数据源=trade_records.metadata_json.entry_gap (入场时已采集, 零新采集线)。",
        data_path="data/ftseek_us.db (trade_records.metadata_json.entry_gap)",
        config_key=None,
        min_n=30,
        graduation_criteria="预注册 (2026-08-29): n≥30 且 拦截组(aligned_gap≥5%)笔均PnL<0 且 "
                            "单侧符号检验 p≤0.05 → 毕业转硬门; 否则继续攒样不重置, 随周检机械判定。",
        eval_script="scripts/gap_chase_gate_eval.py",
        created="2026-08-29",
        last_evaluated="2026-08-29",
        verdict_history=[{"date": "2026-08-29", "verdict": "BASELINE_KEEP_SAMPLING",
                         "evidence": "注册基线(7/1起): 拦截组 n=10 WR=50% 笔均-$28.3 累计-$283; "
                                     "对照组 gap<2% n=252 笔均-$3.6。n<30 且亏损笔数5/10未达显著 → 攒样中"}],
        notes="计数口径=拦截组交易笔数 (eval 脚本从 DB 现算); 与 gap_dive_halve (逆势深跌减半) 互补不对称: "
              "本项管顺向追极端缺口。",
    ),
    ShadowItem(
        id="da_glm_shadow",
        name="DA glm-5.2 影子攒样 (SHORT 产出意愿)",
        status=ShadowStatus.OBSERVING,
        description="2026-08-30 用户裁'1要' (05a69c43 落地), 2026-08-30 第9轮 9.1 补注册。"
                    "检验 8/30 多模型对比中 glm-5.2 的唯一显著分歧: 生产 4/4 里唯一给 SELL_SHORT 的是 glm。"
                    "架构纯旁路: 读生产 bridge 候选池 → 独立 collect_all (输入同构) → "
                    "glm-5.2 分析师 Pass1 (生产同款 SYSTEM_PROMPT, temp=0.3) → append jsonl。"
                    "零落库/零 bridge 写/零指令触碰。9.6: 材料漂移>1.0pp 标 tainted 剔除分歧归因。",
        data_path="data/shadow/da_glm_shadow.jsonl",
        config_key=None,
        min_n=10,
        graduation_criteria="预注册 (窗口 8/31-9/14, 10 交易日): "
                            "①JSON 成功率≥90% ②glm SELL_SHORT 提案≥5 笔 → PROMOTE_PROPOSAL 另立 DEB; "
                            "任一不过 → ARCHIVE 结案。③SHORT 提案 T+1 方向均值>0 仅当 n≥20 出结论 "
                            "(9.5 修正: 原 n≥5 ≈ 抛硬币), n<20 只作立项参考不作证据读。"
                            "9/14 由 eval 脚本机械裁决, 周检可见。",
        eval_script="scripts/da_glm_shadow_eval.py",
        created="2026-08-30",
        last_evaluated=None,
        verdict_history=[{"date": "2026-08-30", "verdict": "BASELINE_NO_DATA",
                         "evidence": "补注册基线: 现仅 1 行 run_mode=test (COIN 干跑复现分歧 "
                                     "prod=LONG vs glm=SELL_SHORT, trig=2, finish_reason=stop), "
                                     "无 live 样本; 8/31 起攒样"}],
        notes="计数口径=live 行数 (count_mode lines); cron '30 20 * * 1-5' (timeout 9000, "
              "9.2 修正自 3000) 是唯一采集线也是唯一 kill switch; "
              "判据③降级记录见 scripts/da_glm_shadow.py docstring (9.5)。",
    ),
    ShadowItem(
        id="e2_momentum",
        name="E2 入场动量确认",
        status=ShadowStatus.READY_TO_EVALUATE,
        description="入场后 15min MFE 确认 (θ=0.5%) 反事实。record-only 不拦。毕业人群=前向影子(8/3起)。",
        data_path="data/e2_momentum_shadow.jsonl",
        config_key=None,
        min_n=30,
        graduation_criteria="DEB-20260807 §5.1: 拦截组净亏+放行组盈利模式成立 → 提请用户裁决转硬门; 统计按宪章第9律日聚类+MDE",
        eval_script="scripts/e2_momentum_graduation_gate.py",
        created="2026-07-20",
        last_evaluated="2026-08-23",
        verdict_history=[{"date": "2026-08-23", "verdict": "PATTERN_DIRECTIONAL",
                         "evidence": "前向n=129: 拦截n=80净-$1589.6(均-$19.87,WR33%) vs 放行n=49净+$1221.7(均+$24.93,WR65%); 日聚类15配对日 diff=-$34.8 t=-2.67<MDE$51.6 → 方向强但不可分辨。字面判据(拦截组全亏)不成立(WR33%)。转硬门与9月退出参数冻结冲突, 建议10月初裁。"},
                        {"date": "2026-08-23", "verdict": "USER_DEFERRED",
                         "evidence": "用户裁决'同意': 不转硬门, 压至10月初(冻结窗口结束后)再裁; 影子继续攒样不重置, 周检机械跟踪。"}],
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
        notes="毕业后生产表现由 RFC-067 台账 + overnight_arm_report 预注册判据跟踪",
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
    ),

    # ── 退出策略核查批 (2026-08-31, 用户裁"影子先行") ──
    ShadowItem(
        id="reentry_quick_tp_gate",
        name="再入场快TP门禁吞掉",
        status=ShadowStatus.OBSERVING,
        description="8/7 裁决的再入场快收 TP (RR≈0.58) 被 7/10 min_tp_rr=1.5 门禁静默吞掉, "
                    "13 笔实弹 0 笔 TAKE_PROFIT。影子记'价格触及被吞TP'反事实 + 平仓结算差, "
                    "回答'恢复快TP还是废quick_tp参数'。两次裁决互杀, 2026-08-31 退出核查发现。",
        data_path="data/shadow/reentry_quick_tp_shadow.jsonl",
        config_key="playbook.reentry_quick_tp_shadow_enabled",
        min_n=10,
        graduation_criteria="窗口 9/1-9/30: n≥10 笔 TP 触达结算行 → tp_vs_actual_pct 均值>"
                            "费用线(~0.15%) ∧ ≥70%方向为正 → 提案恢复快TP(门禁按 decision_id "
                            "前缀豁免, 另立DEB+用户裁); 否则维持门禁+提案废 quick_tp 参数。"
                            "窗内禁改门禁/快收参数。",
        eval_script=None,
        created="2026-08-31",
        notes="count_mode=lines; 结算行含 is_reentry/touch/tp_vs_actual_pct; "
              "DEB-20260831-reentry-quick-tp-gate-conflict §四; 判据字典 B13",
    ),
]


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
        if s.status == ShadowStatus.OBSERVING and s.current_n >= s.min_n:
            s.status = ShadowStatus.READY_TO_EVALUATE
            notifications.append(
                f"🔔 影子 [{s.id}] {s.name} 样本已达 {s.current_n}/{s.min_n} → "
                f"READY_TO_EVALUATE, 待用户裁决。判据: {s.graduation_criteria}")
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
                        continue
                seen.discard(None)
                return len(seen)
            return sum(1 for _ in open(p, encoding="utf-8") if _.strip())
        except Exception:
            return None
    if path_str.endswith(".json"):
        try:
            d = _j.loads(p.read_text(encoding="utf-8"))
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
