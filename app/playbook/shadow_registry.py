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
from pathlib import Path
from typing import Optional
from enum import Enum

# 挂载核对哨兵的 cron 解析面 (2026-09-01 复查收口): /etc/cron.d 内含点号文件名同样被
# Alinux8 cronie 执行 (401bedcb 僵尸 cron 38 天教训), 核对面必须覆盖目录内全部文件,
# 不能只看 crontab -l。测试可 monkeypatch 这两个常量注入临时目录。
_ETC_CRONTAB = Path("/etc/crontab")
_ETC_CRON_D = Path("/etc/cron.d")


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
    # 采集退役标记 (2026-09-21): True = 该通道**不再采集** (由采集脚本 _collector_gate 执行)。
    # 刻意与判据状态分开: 判据退役 (KILLED/FAIL) **不等于**没有消费者 —— 实锤 capital_flow
    # 的原料在喂生产门 (intraday_reentry 盘中资金背离门), 若按判据状态自动停采会断料。
    # 停采必须**显式**声明 (本字段或该通道独立开关), 并在 notes 写理由; 数据一律保留不删。
    collection_retired: bool = False
    notes: str = ""
    # 计数口径: "lines" (默认, jsonl 行数) | "distinct:<field>" (按字段去重计数,
    # 用于首日回补大量历史数据的事项 — 回补行只算 1 个样本, 防立即超 min_n
    # 触发永久 READY 通知风暴; 2026-08-29 futu_channel_iv_hv 落地时引入)
    count_mode: str = "lines"
    # eval 执行方式: False=应有 cron/调度挂载 (挂载核对话语权); True=按需手工/
    # 裁决日触发 (哨兵跳过挂载核对, 2026-09-01 挂载核对哨兵引入)
    eval_on_demand: bool = False
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
        # 2026-09-21 用户裁 (程序性, 批准记录见 handoff 当日下午增量): 闹钟设 9/30
        # (判据字典 B13 禁改窗结束日), 到期一次性结算用途; 闹钟前不做任何实质改动、
        # 不启动反向池论证。needs_user 保持 True = 9/30 结算仍需用户裁用途三选一。
        "read_at": "2026-09-21", "recheck_after": "2026-09-30", "needs_user": True,
        "note": "榜内横截面 IC=-0.44/t=-13 (榜首续跌) 与笔级 top10 +$15.20/笔 符号相反 ⇒ 用途待裁 "
                "(RFC-092 §8.2/§9.1)。用途三条路 (排除/反向池/并入 kr3) 均需另立预注册。"
                "2026-09-21 用户裁: 9/30 窗满后一次裁, 在此之前维持现状 (榜单继续做参考层输入)。",
    },
    "reentry_quick_tp_gate": {
        "read_at": "2026-09-21", "recheck_after": "2026-09-30", "needs_user": False,
        "note": "n=13/10 达标; 判据点名 9/1-9/30 窗内禁改, 到期一次性结算 (判据字典 B13)。",
    },
    "gap_chase_gate": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "n=31 ✓ / 笔均 -16.6 ✓ / 符号检验 p=0.93 ✗ ⇒ 3 项过 2, 继续攒样 (判据字典 B1)。",
    },
    "vwap_gate_ab": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "补齐按日聚类后被拦组 +0.176% 但 MDE±0.39 ⇒ 不可分辨, 不改门 (§8.4)。",
    },
    "entry_delay_confirm": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "全样本过线但前瞻子样本全不过 + 日聚类 MDE±7.95 ⇒ 不可分辨, 不呈裁。",
    },
    "exit_disable_cf": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "每规则 MFE n=31<50 / DEAD n=33<50 ⇒ 每规则 n 门未达。",
    },
    "extreme_gap_ban_shadow": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "被拦 n=58 净 -1826 ✓ 但误杀率 53%>40% ✗ ⇒ 判据未过, 维持纯观测。",
    },
    "gap_alignment": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "各格 n=5/10/22/9… 多 <30 ⇒ 格内 n 门未达。",
    },
    "intraday_loss_breaker_a_prime": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "被挡笔期望 +45.0 (门 ≤-10, 方向相反) 但 forward 仅 14<30 ⇒ 按已记录规则不下结论。",
    },
    "kr3_direction_runup_gate": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "LONG blocked n=36 均值 -1.9/中位 +1.1 (尾部驱动); SHORT blocked n=16<30 ⇒ 延长观察。",
    },
    "penetration_shadow": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "基线穿透率 97.4%; 假设集 None (未调 LLM) ⇒ 不判 (判据字典 B17)。",
    },
    "premarket_invalidation": {
        # 2026-09-21 用户裁「口径随实现」(批准记录见 handoff 当日下午增量):
        # 判据文字"盘前口径增量"更正为"开盘后早段盘中 (09:35–09:59 ET) 口径"——
        # B 规则判"开盘后跌>5%"事件, 盘前相位不存在该事件, 实现相位本就正确, 系登记文字过窄。
        # 改判据=重新裁决留痕: graduation_criteria 同步更正 (下方条目内【口径更正】标记);
        # B 已攒 n=4 均为盘中相位采集, 更正后口径下有效, 不清零继续攒样至 n>=20。
        # "盘前数据独立增量"成为新问题, 若要问须另立预注册 (本次未开)。
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "A/C 已按 DEB-20260911 处置 RETIRED_NO_PROMOTION; B 执行 n=4<20 ⇒ 攒样中。"
                "相位口径已定谳 (2026-09-21 用户裁): 开盘后早段盘中 09:35–09:59 ET。"
                "实测证据: 产物 396 条全部落在 09:35–09:47 ET (RFC-092f,"
                " docs/analysis/2026-09-21-realtime-price-authority-audit.md §3)。",
    },
    "da_blocked_cf": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "T+1 mean -0.051% 未越 ±0.15% ⇒ KEEP_SAMPLING (判据字典 B14)。",
    },
    "futu_channel_iv_hv": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "三桶无残存效应 (medium +35.74 但 MDE±52 不可分辨; high 无效应) ⇒ NO_RESIDUAL (§8.1)。",
    },
    "futu_channel_ofi_baseline": {
        "read_at": "2026-09-21", "recheck_after": "2026-10-21", "needs_user": False,
        "note": "只测到 capital_flow 相对它无增量 (-0.1192); OFI **自身** IC 未单独测 ⇒ 无 KILL 依据。",
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
              "2026-09-01 裁杀: 5 天 3 样本=触发近乎不存在, 攒样无意义",
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
        verdict_history=[{"date": "2026-09-21", "verdict": "KILLED",
                          "evidence": "清账批 (RFC-092): 判据产物 data/capital_flow_replay_verdict.json "
                                      "(2026-08-22) 四关门 all_gates_pass=False — inflow t=-0.48 / "
                                      "outflow t=-0.71 均不显著, IS/OOS 异号, placebo p=0.85; "
                                      "且用户已裁'不得拦单' ⇒ 该假设已判死, 注册表未同步。归档退役。"}],
        notes="capital_flow_replay_verdict: 四关全不过, 用户裁决不得拦单。"
              "2026-09-21 清账: 已裁未归档 → 退役, 退出待裁队列; "
              "采样行仍由 playbook.execution_parameters.falsification_gate_capital_flow_shadow "
              "在写 (本项退役不改采写行为, 停采需另关开关)。",
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
        status=ShadowStatus.OBSERVING,
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
                                      "本项只保留其特有面 (榜内横截面 IC, kr3 不覆盖) 继续观测。"}],
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
              "本项管顺向追极端缺口。",
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
        notes="2026-08-29 注册表 verdict 'PROD_KILL_CRITERION_REGISTERED' 的执行落地; "
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
        config_key="playbook.execution_parameters.reentry_quick_tp_shadow_enabled",
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
              "stop_loss_at_arm=初始SL口径(生效SL更紧, SL_touch 偏少知悉); "
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
        notes="首个生产夜=9/1 盘 (结构化字段首夜); 8/31 指令无结构化字段重放 n=0 合预期; "
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
              "(390 回填系合法历史样本=§0.3 证据基线, 非刷样); by_source 切片读 latest summary",
        count_mode="distinct:trade_id",
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
              "脚本自带 MarketDataRouter 拉日K, 盘后任意时点可跑",
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
        last_evaluated="2026-09-21",
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
                                      "结构建议留待 forward n≥30 重测时一并呈裁。"}],
        notes="口径: trade_mode=simulate 且 pnl_net NOT NULL, ET 日, exit 事件流 "
              "(tie 时 exit 先结)。持久化=ftseek_us.db 派生表 cb_shadow_daily/"
              "cb_shadow_blocked (全量重建幂等) + JSON 汇总。首日回补即证伪预验证量级 "
              "(entry 序累加=前视偏差): 全期 n=191 期望 +46.90 主判据不过, 近30天 "
              "n=4 +254 方向相反 -> backfill 判不毕业, 价值转 forward 攒样; 详见 "
              "DEB-20260904 §11.6(a)。注册表计 n=全期 settled 被挡笔, 首日即 >=min_n "
              "属预期 (判据主门是期望非 n, READY 不等于毕业)。",
    ),
    # ── KR3 预注册假设影子 (采集 9/2 上线 cron, **2026-09-06 补登记** — 未登记=无人跟踪) ──
    ShadowItem(
        id="kr3_direction_runup_gate",
        name="KR3 方向×涨幅证伪门 (RFC-072, 9 月唯一预注册假设)",
        status=ShadowStatus.OBSERVING,
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
        last_evaluated="2026-09-13",
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
                        ],
        notes="计数口径 = jsonl 行数 (trade_id 去重); **min_n=30 数的是 would-block 子集已结算笔数, "
              "不是总行数** ⇒ 注册表 READY 不等于可裁决, 裁决批须按子集 n 判 (2026-09-06 面板实测 "
              "子集 n=23/30)。判分代码 (按日聚类 t / MDE / OOS) 按 RFC-072 §六.3 **不得预先实现** — "
              "「期末裁决为独立批次, 判分口径现写现验, 防先写判分再凑数据的选择压力」; 故 eval_script "
              "只出稳健性视角 (均值/中位/胜率/尾部), 不出预注册判据值, 这是设计不是缺口。"
              "eval_on_demand=True: 报告脚本无 cron (手动/裁决日触发), 只有采集器挂 cron "
              "(crontab 10 6 * * 2-6, 台账 §2.3)。回滚=删本条目 + 删 cron 行 + 删 jsonl (纯观察件)。",
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
              "= 心跳由 52 5 cron 保证。回滚=删本条目 (读数小节属 unit_cell 脚本, 另案)。",
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
        name="SHORT 逆势追空禁入影子 (触发时刻口径)",
        status=ShadowStatus.OBSERVING,
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
                            "(n 口径 = 命中行数 count_mode=where:over_threshold=True, 扫描行"
                            "不计入; 判据须用扫描行作对照臂: 命中组 vs 未命中组净额差 + 方向"
                            "一致率, 单看命中组无 IC 意义 — 2026-09-18 补)",
        eval_script=None,
        created="2026-08-08",
        verdict_history=[
            {"date": "2026-08-08", "verdict": "HARD_GATE_REJECTED_OLD_BASELINE",
             "evidence": "DEB-20260808 §8.3: post-blackout(1015) 基线残存效应 −$19.80 "
                         "(8月逆势亏单全在9点档, 被禁触窗架空) → 不落硬门, 仅 shadow 记录"},
            {"date": "2026-09-11", "verdict": "SSOT_FOR_SHORT_CHASE_INTENT",
             "evidence": "DEB-20260911: 禁触窗 8/15 已改 0935 ⇒ 旧否决前提失效, 假设在新基线"
                         "下未检验; A_short_chase 快照规则退役, 假设并入本项作单一证据源 "
                         "(A 的 310 行 IC: SELL_SHORT rho=-0.378 p≈0.005 方向成立, 但载体"
                         "相位须为触发时刻); A 快照行**不得独立转正**"}],
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
              "(任务 #9)。回滚 = short_chase_ban_shadow_enabled 置 false。",
        eval_on_demand=True,
        intent="short_chase_ban",
        cross_ref=("premarket_invalidation",),
    ),
    ShadowItem(
        id="ue_tier_gate",
        name="UE tier 闸影子 (PROPOSAL-20260905 U3/P4, 9/11 补结算器)",
        status=ShadowStatus.OBSERVING,
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
        notes="count_mode=distinct:date (每扫描日 1 行, 行数=交易日数, 与 >=15 交易日判据同轴)。"
              "cron 52 5 * * 6 (fwd 回填 05:30 后, 与 ue_tier_shadow_eval 05:50 同窗)。"
              "与 tier_gate_enabled 互锁: 影子期禁止打开 (config 注释在案)。回滚=删 cron 行+结算器+本条目。",
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
        last_evaluated="2026-09-15",
        verdict_history=[
            {"date": "2026-09-15", "verdict": "BASELINE_NO_DATA (挂环当日 1/10 轮次)",
             "evidence": "挂环当日 4 组读数 (ET 盘前同一批 5-7 标的): 交易所时戳可得率 5/5→7/7 "
                         "=100%; genuine 龄 1.5–159.1s (AXON 4 次观测 2 次超 90s ⇒ **90s 阈值在盘前"
                         "阵发报价下不成立**); alias 洗白真陈旧 1/5 (AXON 别名 −10s'很新鲜' / 交易所 "
                         "159.1s 已超阈)。机械裁决器首跑 = INSUFFICIENT_RECORD (有效轮次 1/10, "
                         "P95 41.8s 仅供参考) ⇒ 禁止建议激活。"}],
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
        graduation_criteria="相位标定覆盖率稳定 + 分档方向一致性成立 ⇒ 提案转正; 与 RFC-099 §5 判据逐条对账",
        eval_script="scripts/earnings_direction_check.py",
        created="2026-09-23",
        notes="零下单; 相位来源 futu 日历 ∪ Finnhub (并源), 空相位实测 11.8%。",
    ),
    ShadowItem(
        id="eth_ext_entry_prefilter",
        name="入场判据①②对照影子",
        status=ShadowStatus.OBSERVING,
        description="现行判据 vs 对照判据在**同一批候选**上的裁决对照 (旧判据在生产误拦了多少)。只记账。",
        data_path="data/ext_entry_prefilter_shadow.jsonl",
        min_n=30,
        graduation_criteria="对照显示旧判据误拦持续存在且方向一致 ⇒ 提案切换判据; 影子与生产裁决不一致率需可解释",
        eval_script="scripts/eth_stage_daily_report.py",
        created="2026-09-23",
        notes="观测与决策解耦: 判据评估已前移到涨幅带/量闸之前, 否则样本被下游闸截断。",
    ),
    ShadowItem(
        id="eth_short_probe",
        name="盘前做空通道探针",
        status=ShadowStatus.OBSERVING,
        description="盘前做空通道可达性实证 (能下单/能成交/能建空仓/能平回), 1 股 SIM, 每日 ≤3 次。",
        data_path="data/eth_short_probe.jsonl",
        config_key="EXT_ENTRY_ALLOW_SHORT",
        min_n=3,
        graduation_criteria="见 RFC-099 §10 五条回滚触发; 复核日 2026-10-07 (逐日 attempt 上限=额度, 非样本量目标)",
        eval_script="scripts/probe_eth_short_open.py",
        created="2026-09-23",
        notes="选标按价差最窄且 ≤1%, 无合格返回 no_liquid_candidate (不下一张注定不成交的单)。",
    ),
    ShadowItem(
        id="eth_premarket_exit_shadow",
        name="盘前退出影子",
        status=ShadowStatus.OBSERVING,
        description="盘前持仓的逐分钟退出判据读数 (利润保护/亏损侧只记不动), 逐分钟一行。",
        data_path="data/premarket_exit_shadow.jsonl",
        min_n=30,
        graduation_criteria="水下保本族/时间退出族净额>0 且样本 ≥30 ⇒ 提案; 与 EXT_CERT_LOCK_PCT 现行裁决对账",
        eval_script="scripts/extended_hours_exit_cron.py",
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
        eval_script="scripts/extended_hours_exit_cron.py",
        created="2026-09-23",
        notes="盘后窗 16:00–19:59 ET; 收口强平 EXT_PREMARKET_FLATTEN_FROM=925 只作用于当日进场仓。",
    ),
    ShadowItem(
        id="eth_scan_state_cache",
        name="扫描状态缓存 (RFC-101 L2) 观测",
        status=ShadowStatus.OBSERVING,
        description="跨轮昂贵门结论复用: 命中/到期/失效/省调用 计数 (开关默认关, 影子档只记账)。",
        data_path="data/loops/ext_entry_scan/state.json",
        config_key="EXT_SCAN_STATE_CACHE",
        min_n=14,
        graduation_criteria="复核日 2026-10-08: 漏票反事实=0 + 观测行无缺行 + 命中率不过高 (失效判据未失灵) ⇒ 允许常开",
        eval_script="scripts/eth_stage_daily_report.py",
        created="2026-09-23",
        notes="与 RFC-100 同期读数; 回滚=两开关置 false + 删 data/loops/ext_entry_scan/。",
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
        if age > _STALE_DAYS:
            warnings.append(
                f"🪝 产出超龄 [{s.id}] {s.name}: {p.name} 已 {age} 天未更新 "
                f"(阈值 {_STALE_DAYS} 天, 疑似采集断流)")
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
