"""入场延迟确认门 (D2' forward, RFC-141) — 触发后让子弹飞 N 秒再进场。

════════════════════════════════════════════════════════════════════════
为什么存在
════════════════════════════════════════════════════════════════════════
SL_INITIAL 出血族 (进场即反向, MFE=0, ~17 分钟被杀) 是 playbook 最大单一
亏损族: 近窗 6 笔 −$898, 全史 −$2,963。影子回放 (DEB-20260909 §5.2 /
scripts/entry_delay_confirm_shadow.py) 实测「触发后延迟 2min 复核, 价格仍站
触发方向才进场」对该族避免率 65.5%、误杀 0、日聚类 t=4.92 —— 但那是
**事后 exit_reason 标签人群**, live 门无法事前识别 (T19 红线: exit_reason
不得进门前置)。可事前识别的分层 R1 (SHORT×开盘窗) t=1.94 未达门, 属后验
假设 ⇒ 必须前瞻攒样 (DEB-20261009 §31.4)。

本模块 = 那个前瞻采样器, 且是 **SIM 实弹形态**: 直接改 SIM 交易行为, 结果看
富途 app 上的真实 SIM 盈亏。2026-10-10 曾全量走门；2026-11-05 用户裁重开后
现役 = `120s + holdout_pct=20 + mid/grace`，新 cohort 自当日起重计。
**现役机制仍是 UNPROVEN，不得把归档机制读数写成已确认盈利优势。**

现役 grace 的精确语义也不是 60/90/120s 严格 2-of-3：120s 首见逆向只启动
grace 且不消耗票，后续两次逆向才 ABANDON；任一后续回正立即 CONFIRM。
mid 任一端不可用时回落 legacy 并留痕，因此 cohort 必须按 ruler_used 分解。

════════════════════════════════════════════════════════════════════════
设计约束 (每条都有实测依据)
════════════════════════════════════════════════════════════════════════
1. **锚 = arm 时刻的 reprice 执行锚**, 不是盘前计划锚。
   实测 (dual_anchor 影子 573 行): 两锚分歧中位 2.10%, 60.4% 的行分歧 >2%,
   trigger_divergent 216/573 (38%) ⇒ 选错锚会翻转超三分之一的触发判定。
   影子裁决基准 = trade_records.entry_price ≈ 执行锚 + 滑点, 故 live 门
   必须用执行锚才是同一把尺子。「哪个锚更该信」归 RFC-123 阶段1, 本门不抢跑。

2. **判定在 10s 快循环, 不在 180s 主循环。**
   影子只裁过 2min/5min 两条腿 (DELAYS_MIN=(2,5)); 主循环实测 180s ⇒ 挂主循环
   等于静默落地一个 3-5min 的**未预注册变体** (DEB-20261009 §27 缺陷1 同型)。
   快循环 interval=10s ⇒ 确认时点 = arm+120s 后首次 sweep, 误差 ≤10s。

3. **不碰 execution_state。** 那是 broker 镜像状态机
   (PENDING→UNKNOWN→SUBMITTED→FILLED_UNBOOKED→BOOKED),
   position_sync._reconcile_playbook_execution_states 按它做账实对账
   (幽灵仓根因修复)。加枚举值 = 动账实对账面。改用 metadata_json 旗标
   (merge_instruction_metadata 正是为「该落库但不值得加列」而设)。

4. **pending 期间不 reprice 该指令** (auto_execute 侧 continue 在 reprice 之前)
   ⇒ 锚不漂、DB 列不被覆盖、零写库污染。CONFIRMED 后放回正常流程, 由下轮
   auto_execute 按当时市价 reprice 成交 —— 与影子口径一致 (判定用原锚,
   成交用延迟后市价, opp_cost = 两者差)。

5. **重启孤儿自愈**: pending 表是进程内存态 (生命周期仅 ~120s), 重启即空。
   metadata 旗标残留 PENDING_CONFIRM 而内存无对应项时: 窗口未过 ⇒ 按旗标
   (锚/方向/armed_at/delay/ruler 均为 arm 时冻结值) 重建 pending 续上门控;
   无可信锚或窗口已整体超 TTL ⇒ 转 HEALED 留痕并恢复正常执行。既不让重启
   把 120s 确认静默降级为立即执行, 也绝不永久卡死 (失败路径纪律)。

6. **fail-open 到「照常执行」**: 本门任何异常 ⇒ 放行原路径, 不下单不是
   安全方向 (会静默减少样本 + 改变基线)。与 chase_cap 门的 fail-close 相反,
   因为那个门拦的是危险单, 这个门拦的是普通单。

判据 (预注册于 docs/decisions/RFC-141 §4, 读数前写死, 裁决日不挪):
  裁决源 = **开门后 SIM 真实盈亏 vs 门前预注册基线** (2026-10-10 用户裁改口径,
  jsonl 降级为机制留痕, 不独立裁决)。样本门 n≥60 ∨ ≥10 交易日。
  ①病灶族收敛: SL_INITIAL 族净均 ≥−$40/笔 (v2 基线 −$162.3363, −$40 取严) 或族 n≤1
  ②开盘窗改善: 09:35–09:45 净均 >−$2.2632 ∧ 日聚类 t >−0.3218
  ③机制量: 避免率 ≥30% ∧ CONFIRMED opp_cost <0.3%
  ④不伤赢家: 非 SL_INITIAL 族全量净均 ≥+$7.79 (v2 基线 +$9.7353 的 80%),
    裁决口径为剔费净均 ≥+$8.79 (v2 基线 +$10.9854 的 80%)
  R1 分层 (SHORT×09:35-09:45) / LONG×开盘窗 只印不判。
  ⚠️ 基线数字由 scripts/settle_entry_confirm_forward.BASELINE 冻结, t 用
  statistics.stdev (ddof=1) 与 _day_clustered_t 同尺; 改动须先改 RFC 预注册文本。

════════════════════════════════════════════════════════════════════════
v2 判尺与持续性修订 (2026-10-15 用户裁「直接优化落地」, RFC-141 §5.10)
════════════════════════════════════════════════════════════════════════
v1 前瞻 54 行实证两个病灶: ①混尺 — 锚=ask/bid(成交侧) 复核=last trade, 价差跳动
被当反转 (INTC +1.6bp 即弃单); ②一次性 — 单点采样定生死, 指令当日无再生通道。
v2:
  · 判尺 mid-vs-mid (两端 bid/ask 有效才用; 否则该笔回落 legacy 混尺 + ruler_used 留痕);
    opp_cost_pct 定义不变 (执行锚×last), 判据③尺子连续。
  · grace 2-of-3: 到点首见逆向进 grace (≤3 次 sweep, ≥2 次逆向才弃; 任一同侧即确认;
    取价失败不消耗次数; 耗尽仍不足票 ⇒ 确认放行, fail toward trade 与 fail-open 同向)。
  · 弃单后 +5m/+10m 补记 type=entry_confirm_post (纯观测, 不进 _agg), 裁决日分解
    真避免 vs 误杀。
回滚 = entry_confirm_ruler: legacy + entry_confirm_grace_sweeps: 0 + restart (v1 逐字行为)。
⚠️ §5.9 回放曾判「多探测无净 edge」但测的是旧混尺; mid 尺×持续性的交互未覆盖,
   v2 前瞻样本即首次实测 (§5.10 留有该张力的强制披露)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 常量 (配置可覆盖, 见 confirm_gate_config) ──────────────────────────
DEFAULT_DELAY_SEC = 120          # 影子裁过的腿: DELAYS_MIN=(2,5) 中的 2min
DEFAULT_HOLDOUT_PCT = 20         # 对照臂比例 — ⚠️ 仅「缺键回落」默认, 生产已置 0
                                 # (2026-10-10 用户裁全量走门, 见 config 注释与 RFC §2)
DEFAULT_TTL_SEC = 600            # pending 超期即弃 (防跨日/卡死)
# [RFC-178] 孤儿 (旗标 PENDING 但内存无记录, 且窗口不可重建) 允许补 arm 的次数。
#   原行为是直接转 HEALED 照常执行 = 确认门在最需要它的时刻被跳过 (fail-open)。
#   证据: 2026-09-15→10-30 全库仅 3 次孤儿自愈, 其中 2 次最终 OVERNIGHT_GAP_STOP
#   (US.CAT 10-14 -302.5 / US.CRM 10-27 -356.1), 1 次 +13.6 —— n=3 不足以定因,
#   但"没确认就放行"逻辑上不成立。补 1 轮 (delay_sec 后重估) 仍无报价才放行,
#   既有兜底不摘 (不能让无报价标的把指令永久卡死)。
ORPHAN_REARM_MAX = 1
# ── v2 (2026-10-15 用户裁, RFC-141 §5.10): 判尺 + 持续性 ──
DEFAULT_RULER = "mid"            # mid-vs-mid 同尺; "legacy" = v1 混尺 (锚 ask/bid × last)
DEFAULT_GRACE_SWEEPS = 3         # 到点首见逆向后的 grace 复核次数 (10s 间隔)
DEFAULT_GRACE_VOTES = 2          # grace 内 ≥N 次逆向才 ABANDONED; 0 次 sweep = v1 单点
# ── RFC-153 (2026-10-20 用户批准): FADE_SHORT 开盘快跌禁追 ──
#   FADE_SHORT 的本意是等反抽再空; 确认窗内价格已顺向快跌 ≥1% 时, 确认门实际上
#   变成了追空。开盘 15 分钟内该形态左尾集中 (10-20 LITE/NBIS 双 SL_INITIAL),
#   故到点直接放弃, 不进 grace。只作用于 FADE_SHORT, 其余 archetype 零改动。
DEFAULT_FADE_SHORT_MAX_OPP_PCT = 1.0
DEFAULT_FADE_SHORT_WINDOW_MIN = 15
# ── RFC-170 (2026-10-26 用户裁「按性价比执行」): 确认侧追价影子 (record-only) ──
#   实证 (n=53 已结算确认单, 2026-10-12~10-23): 确认单追价 opp 均 +0.350% / 中 +0.299%
#   (mid-mid 尺均 +0.365%), 已越过预注册 c3 天花板 0.30%; 且 **3 笔 SL_INITIAL 全在
#   追价最大的三笔** (opp +0.41/+1.07/+1.41%, 净 −141/−149/−148) —— 门从 pass 侧把
#   它本要消灭的出血族放了回来。Spearman(opp, 净额)=−0.264 p=0.056。
#   本常量只做**记录**: 确认时 opp ≥ 阈值 ⇒ 在 forward jsonl 打 would_abandon 标记 +
#   INFO 日志, 不改任何判定。裁决日与门本体同尺比较 (禁止现在改判据 = 阈值购物)。
CHASE_SHADOW_PCT = 0.5           # 单位 %, 与 opp_cost_pct 同尺
_STATE_KEY = "entry_confirm"     # metadata_json 下的旗标键

# 内存 pending 表: inst_id -> arm 快照。有界 (TTL 清理 + 上限防泄漏)。
_PENDING: Dict[int, Dict[str, Any]] = {}
_PENDING_MAX = 200
# v2 弃单后观测表 (纯观测, 不进 _agg): inst_id -> {symbol/direction/anchor/mid_arm/
# abandoned_at/m5/m10}。重启丢失可接受 (观测非交易路径), 有界同上。
_POST_WATCH: Dict[int, Dict[str, Any]] = {}
_POST_WATCH_MAX = 200
_POST_MARKS = (("m5", 300, "+5m"), ("m10", 600, "+10m"))
_POST_GIVEUP_SEC = 900           # 补记放弃线 (防泄漏; 超线价不可得即弃记)
_PATH_SHADOW_DELAY_SEC = 120
_PATH_SHADOW_OFFSETS_SEC = (60, 90, 120)
_PATH_SHADOW_MAX_TIMING_ERROR_SEC = 20

# 产物 (锚定模块路径 — 相对路径影子污染教训, 2026-09-21)
_ROOT = Path(__file__).resolve().parents[2]
_FORWARD_PATH = _ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
# 写盘互斥锁 (2026-11-05): arm 在 auto_execute 线程, 判定在 10s 快循环线程,
#   两者都会写同一 jsonl ⇒ 无锁时可能交错出半行 (整份文件后续解析失败)。
_FORWARD_LOCK = threading.Lock()
SCHEMA_V2 = "entry_confirm_forward_v2"   # v2 起: +ruler_used/mid_arm/mid_move_pct/grace/双边报价
                                         # (v1 旧行识别常量在结算器侧, 门只写 v2)

# 状态枚举 (写进 metadata + jsonl, 供 app/结算器读)
ST_PENDING = "PENDING_CONFIRM"
ST_CONFIRMED = "CONFIRMED"
ST_ABANDONED = "ABANDONED"
ST_EXPIRED = "EXPIRED"
ST_HOLDOUT = "HOLDOUT"
ST_HEALED = "HEALED"

# 放弃单落库用的 skip_reason (进 dashboard「已跳过」区, 与 guardrail 拦截同表现)
SKIP_REASON_ABANDONED = "entry_confirm_reversed"


# ══════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════
# 缺键回落默认 (单一真源; confirm_gate_config 与 _gate_inner 的 cfg 归一并用它,
# 使测试注入的裸 dict 缺新键时也得到 v2 默认 — 与生产同尺, 防两套默认漂移)。
_CFG_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "delay_sec": DEFAULT_DELAY_SEC,
    "holdout_pct": DEFAULT_HOLDOUT_PCT,
    "ttl_sec": DEFAULT_TTL_SEC,
    "ruler": DEFAULT_RULER,
    "grace_sweeps": DEFAULT_GRACE_SWEEPS,
    "grace_votes": DEFAULT_GRACE_VOTES,
    "fade_short_chase_cap_enabled": True,
    "fade_short_max_opp_pct": DEFAULT_FADE_SHORT_MAX_OPP_PCT,
    "fade_short_window_min": DEFAULT_FADE_SHORT_WINDOW_MIN,
}


def confirm_gate_config(pb_cfg=None) -> Dict[str, Any]:
    """读 playbook.execution_parameters.entry_confirm_* (扁平键, PlaybookConfig 拍平面)。

    pb_cfg 非空时走调用方注入的配置对象 (与 auto_execute_flow 内其它门同源同接缝,
    测试可密封); 否则自建 PlaybookConfig (sweep 侧/独立调用)。

    读不到 ⇒ enabled=False (保守关闭, 维持现状基线, 不静默改交易行为)。
    ⚠️ 键路径 = PlaybookConfig._load 拍平的 execution_parameters 节, 与
    entry_open_blackout_enabled 同源; 不可用 get_dotted("playbook.entry_confirm.*")
    (那是嵌套路径, 拍平面读不到 = 装饰性配置)。
    """
    cfg = dict(_CFG_DEFAULTS)

    def _int_or(key, default):
        """⛔ 禁 `pb_cfg.get(k, default) or default` — 会把合法的 0 吃掉。

        2026-10-10 实锤: 用户裁 holdout_pct=0 (全量走门, 不留对照臂稀释 SIM 结果),
        而 `int(get(...) or DEFAULT_HOLDOUT_PCT)` 里 `0 or 20` → 20 ⇒ 配置写着 0、
        生产实际 20% 的单子绕过门, 且 YAML 看着已生效 (静默违背用户意图)。
        只认 None 为「缺键」, 0 是合法值。
        """
        v = pb_cfg.get(key, None)
        if v is None:
            return int(default)
        return int(v)

    def _float_or(key, default):
        v = pb_cfg.get(key, None)
        if v is None:
            return float(default)
        try:
            return float(v)
        except (TypeError, ValueError):
            logger.warning(f"[EntryConfirm] 非法 {key}={v!r} → 回落 {default}",
                           exc_info=True)
            return float(default)

    try:
        if pb_cfg is None:
            from app.playbook.config import PlaybookConfig
            pb_cfg = PlaybookConfig()
        cfg["enabled"] = bool(pb_cfg.get("entry_confirm_enabled", False))
        cfg["delay_sec"] = _int_or("entry_confirm_delay_sec", DEFAULT_DELAY_SEC)
        cfg["holdout_pct"] = _int_or("entry_confirm_holdout_pct", DEFAULT_HOLDOUT_PCT)
        cfg["ttl_sec"] = _int_or("entry_confirm_ttl_sec", DEFAULT_TTL_SEC)
        # v2 (RFC-141 §5.10): 判尺 + grace 持续性。ruler 同样只认 None 为缺键。
        _r = pb_cfg.get("entry_confirm_ruler", None)
        cfg["ruler"] = DEFAULT_RULER if _r is None else str(_r).strip().lower()
        if cfg["ruler"] not in ("mid", "legacy"):
            logger.warning(f"[EntryConfirm] 非法 ruler={_r!r} → 回落 {DEFAULT_RULER}")
            cfg["ruler"] = DEFAULT_RULER
        cfg["grace_sweeps"] = _int_or("entry_confirm_grace_sweeps", DEFAULT_GRACE_SWEEPS)
        cfg["grace_votes"] = _int_or("entry_confirm_grace_votes", DEFAULT_GRACE_VOTES)
        # RFC-153: FADE_SHORT 开盘快跌禁追 (0=关闭; 窗口 0=不启用)
        _fs_enabled = pb_cfg.get("entry_confirm_fade_short_chase_cap_enabled", None)
        cfg["fade_short_chase_cap_enabled"] = (
            True if _fs_enabled is None else bool(_fs_enabled))
        cfg["fade_short_max_opp_pct"] = _float_or(
            "entry_confirm_fade_short_max_opp_pct", DEFAULT_FADE_SHORT_MAX_OPP_PCT)
        cfg["fade_short_window_min"] = _int_or(
            "entry_confirm_fade_short_window_min", DEFAULT_FADE_SHORT_WINDOW_MIN)
    except Exception as _e:
        # fail-safe: 配置不可读 ⇒ 门关闭 (维持现状基线, 不静默改交易行为)
        logger.warning(f"[EntryConfirm] 配置读取失败 → 门关闭 (维持现状): {_e}",
                       exc_info=True)
        cfg["enabled"] = False
    return cfg


def chase_shadow_flag(opp_cost_pct, threshold_pct: float = CHASE_SHADOW_PCT) -> bool:
    """[RFC-170] 追价影子判定 (record-only): 确认时追价 ≥ 阈值 ⇒ 记 would_abandon。

    纯记录, **不参与任何拦截**; 阈值与 opp_cost_pct 同尺 (百分数)。
    进不了判定链的原因: 改判定 = 重新裁决 (预注册纪律), 所以先攒前向样本。
    """
    try:
        return float(opp_cost_pct) >= float(threshold_pct)
    except (TypeError, ValueError):
        return False


def _norm_dir(direction: str) -> str:
    """指令方向 (BUY/SELL_SHORT) → 影子口径 (LONG/SHORT)。"""
    d = str(direction or "").upper()
    if d in ("BUY", "LONG"):
        return "LONG"
    if d in ("SELL_SHORT", "SHORT", "SELL"):
        return "SHORT"
    return d or "UNKNOWN"


def _entry_archetype(inst: Dict[str, Any]) -> str:
    """从 raw_instruction_json 取 archetype; 缺失/损坏 → "" (不启用 RFC-153)。"""
    try:
        raw = inst.get("raw_instruction_json")
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        if isinstance(raw, dict):
            arch = raw.get("entry_archetype")
            return str(arch).upper() if arch else ""
    except Exception:
        return ""
    return ""


def _supply_selection(inst: Dict[str, Any]) -> Dict[str, Any]:
    try:
        raw = inst.get("raw_instruction_json")
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        if isinstance(raw, dict) and isinstance(raw.get("supply_selection"), dict):
            return dict(raw["supply_selection"])
    except Exception:
        return {}
    return {}


def _planned_entry(inst: Dict[str, Any]) -> Optional[float]:
    """DA 计划锚: raw.planned_entry 优先, 回退 confirmation.price_level.

    执行锚 (reprice) 与计划锚可能相差数个百分点; 本字段只记录, 不改门判定。
    """
    try:
        raw = inst.get("raw_instruction_json")
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        if not isinstance(raw, dict):
            return None
        for candidate in (raw.get("planned_entry"),
                          (raw.get("confirmation") or {}).get("price_level")
                          if isinstance(raw.get("confirmation"), dict) else None):
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                value = None  # 候选字段缺失/不可解析 → 试下一个候选 (纯容错, 不告警)
            if value is not None and value > 0:
                return value
    except Exception:
        return None
    return None


def _plan_gap_pct(anchor: float, planned: Optional[float]) -> Optional[float]:
    if not planned or planned <= 0 or not anchor:
        return None
    return round((float(anchor) - float(planned)) / float(planned) * 100.0, 3)


def _plan_gap_shadow(arch: str, gap: Optional[float], cfg: Dict[str, Any]) -> bool:
    """record-only: 均值回归 archetype 的执行锚偏离计划锚 ≥ 阈值时留痕.

    2026-10-21 冲突审计: VWAP/entry-confirm 目前按执行锚判定, 计划锚偏离只记录;
    本标志供 forward 结算按 plan_gap 分桶, 不改变任何入场判定。
    """
    try:
        if not bool(cfg.get("plan_gap_shadow_enabled", True)):
            return False
        threshold = float(cfg.get("plan_gap_shadow_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        return False
    return bool(
        str(arch or "").upper() in ("PULLBACK_LIMIT", "FADE_SHORT")
        and gap is not None and abs(float(gap)) >= threshold
    )


def _holdout_bucket(inst_id: int, pct: int) -> bool:
    """确定性分流 (同 inst_id 恒定同臂, 跨轮/跨重启一致)。"""
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    h = hashlib.sha1(f"entry_confirm_holdout:{inst_id}".encode()).hexdigest()
    return (int(h[:8], 16) % 100) < pct


# ══════════════════════════════════════════════════════════════════
# 判据 (逐字对齐影子 _eval_delay, 不自造)
# ══════════════════════════════════════════════════════════════════
def eval_confirm(direction: str, anchor: float, px: float) -> Tuple[bool, float]:
    """(still_triggers, opp_cost_pct) — 与 scripts/entry_delay_confirm_shadow._eval_delay
    同一公式: LONG 需 px≥锚, SHORT 需 px≤锚; opp_cost 方向归一, 正=延迟让你成交更贵。

    ⚠️ 改这里 = 改判据 = 重新裁决, 须先改 RFC-141 预注册文本。
    """
    if direction == "LONG":
        return px >= anchor, (px - anchor) / anchor * 100
    return px <= anchor, (anchor - px) / anchor * 100


# ══════════════════════════════════════════════════════════════════
# 状态读写 (metadata_json 旗标)
# ══════════════════════════════════════════════════════════════════
def read_state(inst: Dict[str, Any]) -> Dict[str, Any]:
    """从指令读 entry_confirm 旗标。无 ⇒ {}。永不抛。"""
    try:
        md = inst.get("metadata_json")
        if isinstance(md, str):
            md = json.loads(md) if md else {}
        if not isinstance(md, dict):
            return {}
        st = md.get(_STATE_KEY)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _write_state(db, inst_id: int, patch: Dict[str, Any]) -> bool:
    """把 patch 浅合并进 metadata_json.entry_confirm。失败只 warn (不阻断交易)。"""
    try:
        return bool(db.merge_instruction_metadata(inst_id, {_STATE_KEY: patch}))
    except Exception as _e:
        logger.warning(f"[EntryConfirm] 旗标写入失败 inst={inst_id} (不阻断): {_e}",
                       exc_info=True)
        return False


# ══════════════════════════════════════════════════════════════════
# 产物落盘 (前瞻样本, 供结算器聚合)
# ══════════════════════════════════════════════════════════════════
def _write_forward_row(row: Dict[str, Any]) -> None:
    """追加一行前瞻样本。字段与影子 _agg_block 契约对齐 (status/still_triggers/
    opp_cost_pct/notional/date), 另加 arm/live 专有字段。写盘失败只 warn。

    2026-11-05: 加模块级锁 — 快循环 sweep 与 arm 分属不同线程, 无锁的
    `open(...,'a') + write` 在多线程下可能交错出**半行 JSON** (行长约 1KB,
    超过单次写原子性的实际保证), 一旦发生整份 jsonl 的后续消费都会解析失败。
    锁只串行化写盘 (~微秒级), 不影响判定与交易。
    """
    try:
        _FORWARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        _line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with _FORWARD_LOCK:
            with open(_FORWARD_PATH, "a", encoding="utf-8") as f:
                f.write(_line)
    except Exception as _e:
        logger.warning(f"[EntryConfirm] 前瞻行写盘失败 (不阻断交易): {_e}",
                       exc_info=True)


# ══════════════════════════════════════════════════════════════════
# auto_execute 侧: arm / 放行 / 孤儿自愈
# ══════════════════════════════════════════════════════════════════
def gate_at_execute(
    inst: Dict[str, Any],
    db,
    reprice_params: Optional[Dict[str, Any]],
    now_et: datetime,
    cfg: Optional[Dict[str, Any]] = None,
    scheduler=None,
) -> str:
    """在 auto_execute 即将下单前调用。返回动作:

      'execute'  → 照常下单 (门关闭 / holdout 臂 / 已确认 / 孤儿自愈)
      'defer'    → 本轮不下单 (已 arm, 等 sweep 判定)

    调用方约定: 'defer' 时 skipped_count += 1; continue —— 且本函数必须在
    reprice 之后、execute 之前调用 (需要 reprice 后的执行锚), 而 pending 复检
    (defer_pending) 必须在 reprice **之前** 调用 (防锚漂)。

    scheduler (v2 新增, 可选): 非空且 ruler=mid 时, arm 瞬间快照 bid/ask ⇒ mid_arm
    (mid-mid 同尺判定的另一端, RFC-141 §5.10); 为空/取价失败 ⇒ 该笔回落 legacy 混尺
    并留痕 ruler_used, 不阻断 arm。
    """
    try:
        return _gate_inner(inst, db, reprice_params, now_et, cfg, scheduler)
    except Exception as _e:
        # fail-open 双层防御: 本函数自身任何异常 (分流/取锚/类型) 都不得阻断交易。
        # 拦普通单不是安全方向, 且静默减样 = 改变基线 (与 chase_cap fail-close 相反)。
        logger.warning(f"[EntryConfirm] gate 异常 → 照常执行 (fail-open): {_e}",
                       exc_info=True)
        return "execute"


def _gate_inner(inst, db, reprice_params, now_et, cfg, scheduler):
    # cfg 归一: 裸 dict (测试注入) 缺 v2 新键时回落 _CFG_DEFAULTS ⇒ 与生产同尺
    cfg = {**_CFG_DEFAULTS, **(cfg if cfg is not None else confirm_gate_config())}
    inst_id = int(inst.get("id") or 0)
    state = read_state(inst)
    st = str(state.get("status") or "")

    # ── 已判定过的终态: CONFIRMED 放行, ABANDONED/EXPIRED 由 defer_pending 拦 ──
    if st == ST_CONFIRMED:
        return "execute"

    # ── 门关闭 ⇒ 零行为变更 ──
    if not cfg.get("enabled") or inst_id <= 0:
        return "execute"

    # ── 孤儿: 旗标说 pending 但内存无 (重启) — 先尝试重建窗口, 重建不了才放行 ──
    if st == ST_PENDING and inst_id not in _PENDING:
        _restored = _restore_arm(inst, state, now_et, cfg, reprice_params)
        if _restored is not None:
            if len(_PENDING) >= _PENDING_MAX:
                _evict_expired(now_et)
            _PENDING[inst_id] = _restored
            logger.warning(
                f"[EntryConfirm] {inst.get('symbol')} 旗标 PENDING 但内存无记录 "
                f"(重启) → 按旗标重建门控窗口: arm={_restored['armed_at'].isoformat()} "
                f"delay={_restored['delay_sec']}s 锚=${_restored['anchor']:.2f} → defer")
            return "defer"
        _rearms = int(state.get("orphan_rearms") or 0)
        if _rearms < ORPHAN_REARM_MAX:
            _q = _current_quotes(scheduler, inst.get("symbol")) if scheduler is not None else None
            _px = float((_q or {}).get("last") or 0)
            _dir = _norm_dir(inst.get("direction")) or str(state.get("direction") or "")
            if _px > 0 and _dir in ("LONG", "SHORT"):
                _fresh = _restore_arm(inst, {**state, "anchor": _px, "direction": _dir,
                                             "armed_at": now_et.isoformat(),
                                             "ttl_sec": int(cfg.get("ttl_sec") or DEFAULT_TTL_SEC)},
                                      now_et, cfg, reprice_params)
                if _fresh is not None:
                    if len(_PENDING) >= _PENDING_MAX:
                        _evict_expired(now_et)
                    _PENDING[inst_id] = _fresh
                    _write_state(db, inst_id, {
                        **state, "status": ST_PENDING,
                        "armed_at": now_et.isoformat(), "anchor": _px, "direction": _dir,
                        "delay_sec": _fresh["delay_sec"],
                        "ttl_sec": int(cfg.get("ttl_sec") or DEFAULT_TTL_SEC),
                        "orphan_rearms": _rearms + 1,
                        "rearmed_from_orphan_at": now_et.isoformat(),
                    })
                    logger.warning(
                        f"[EntryConfirm] {inst.get('symbol')} 旗标 PENDING 且窗口不可重建 "
                        f"(重启/超期/无锚) → 不立即放行, 按现价补一轮确认窗口 锚=${_px:.2f} "
                        f"delay={_fresh['delay_sec']}s → defer (RFC-178)")
                    return "defer"
        logger.warning(
            f"[EntryConfirm] {inst.get('symbol')} 旗标 PENDING 但内存无记录且窗口不可重建 "
            f"(重启/超期/无锚, 已补 arm {_rearms} 次或无报价) → 转 HEALED 并照常执行 (孤儿自愈)")
        _write_state(db, inst_id, {**state, "status": ST_HEALED,
                                   "healed_at": now_et.isoformat()})
        return "execute"

    # ── 首次到达执行点: 分流 arm / holdout ──
    if not st:
        # v2: ruler=mid 时 arm 瞬间快照双边报价 ⇒ mid_arm (mid-mid 同尺的另一端)。
        #   只在 ruler=mid 时取价 (legacy 回滚臂保持 v1 逐字行为, 不多发一次取价)。
        _q_arm = (_current_quotes(scheduler, inst.get("symbol"))
                  if (scheduler is not None and cfg.get("ruler") == "mid") else None)
        if _holdout_bucket(inst_id, int(cfg.get("holdout_pct") or 0)):
            # 对照臂: 照常执行, 只落一行供配对读数
            _holdout_anchor = float((reprice_params or {}).get("entry_price")
                                    or inst.get("entry_price") or 0.0)
            _holdout_planned = _planned_entry(inst)
            _holdout_gap = _plan_gap_pct(_holdout_anchor, _holdout_planned)
            _holdout_arch = _entry_archetype(inst)
            _write_state(db, inst_id, {"status": ST_HOLDOUT,
                                       "armed_at": now_et.isoformat()})
            _write_forward_row({
                "type": "entry_confirm", "schema": SCHEMA_V2,
                "ts": now_et.isoformat(), "date": now_et.strftime("%Y-%m-%d"),
                "inst_id": inst_id, "decision_id": inst.get("decision_id"),
                "symbol": inst.get("symbol"),
                "direction": _norm_dir(inst.get("direction")),
                "arm": "holdout", "status": "ok",
                "still_triggers": True, "opp_cost_pct": 0.0,
                "anchor": _holdout_anchor,
                "planned_entry": _holdout_planned,
                "plan_gap_pct": _holdout_gap,
                "plan_gap_shadow": _plan_gap_shadow(_holdout_arch, _holdout_gap, cfg),
                "plan_gap_threshold_pct": float(cfg.get("plan_gap_shadow_pct", 1.0) or 1.0),
                "entry_archetype": _holdout_arch,
                "supply_selection": _supply_selection(inst),
                "px_at_confirm": None, "notional": _notional(inst, reprice_params),
                "session_min": _session_min(now_et),
                # v2 字段 (holdout 无判定 ⇒ 判定类字段 None, 锚侧报价照常留痕)
                "ruler_used": None, "mid_arm": (_q_arm or {}).get("mid"),
                "mid_at_confirm": None, "mid_move_pct": None, "grace": None,
            })
            return "execute"

        anchor = float((reprice_params or {}).get("entry_price")
                       or inst.get("entry_price") or 0.0)
        if anchor <= 0:
            # 无有效锚 ⇒ 无法判定, 照常执行 (fail-open)
            logger.warning(f"[EntryConfirm] {inst.get('symbol')} 无有效执行锚 → 照常执行")
            return "execute"

        _arm = {
            "inst_id": inst_id,
            "symbol": inst.get("symbol"),
            "decision_id": inst.get("decision_id"),
            "direction": _norm_dir(inst.get("direction")),
            "anchor": anchor,
            "armed_at": now_et,
            "expire_at": now_et + timedelta(seconds=int(cfg.get("ttl_sec") or DEFAULT_TTL_SEC)),
            "notional": _notional(inst, reprice_params),
            "session_min": _session_min(now_et),
            "delay_sec": int(cfg.get("delay_sec") or DEFAULT_DELAY_SEC),
            # v2: 判尺/grace 参数 arm 时冻结 (与 delay_sec 同纪律 — 配置改动不改在途语义)
            "ruler": str(cfg.get("ruler") or DEFAULT_RULER),
            "grace_sweeps": int(cfg.get("grace_sweeps") or 0),
            "grace_votes": int(cfg.get("grace_votes") or DEFAULT_GRACE_VOTES),
            # RFC-153: archetype/阈值同样 arm 时冻结 (在途单不因配置热改而换语义)
            "entry_archetype": _entry_archetype(inst),
            "supply_selection": _supply_selection(inst),
            "planned_entry": _planned_entry(inst),
            "plan_gap_pct": _plan_gap_pct(anchor, _planned_entry(inst)),
            # record-only 计划锚影子参数: arm 时冻结 (在途单不因热改换语义)
            "plan_gap_shadow_enabled": bool(cfg.get("plan_gap_shadow_enabled", True)),
            "plan_gap_shadow_pct": float(cfg.get("plan_gap_shadow_pct", 1.0) or 1.0),
            "fade_short_chase_cap_enabled": bool(
                cfg.get("fade_short_chase_cap_enabled", False)),
            "fade_short_max_opp_pct": float(cfg.get("fade_short_max_opp_pct") or 0.0),
            "fade_short_window_min": int(cfg.get("fade_short_window_min") or 0),
            "mid_arm": (_q_arm or {}).get("mid"),
            "_path_shadow_samples": [],
        }
        if len(_PENDING) >= _PENDING_MAX:
            _evict_expired(now_et)
        _PENDING[inst_id] = _arm
        _write_state(db, inst_id, {
            "status": ST_PENDING, "armed_at": now_et.isoformat(),
            "anchor": anchor, "direction": _arm["direction"],
            "delay_sec": _arm["delay_sec"], "ttl_sec": int(cfg.get("ttl_sec") or DEFAULT_TTL_SEC),
            "ruler": _arm["ruler"], "mid_arm": _arm["mid_arm"],
            "entry_archetype": _arm["entry_archetype"],
            "supply_selection": _arm["supply_selection"],
            "planned_entry": _arm["planned_entry"],
            "plan_gap_pct": _arm["plan_gap_pct"],
            "plan_gap_shadow_enabled": _arm["plan_gap_shadow_enabled"],
            "plan_gap_shadow_pct": _arm["plan_gap_shadow_pct"],
            "fade_short_chase_cap_enabled": _arm["fade_short_chase_cap_enabled"],
            "fade_short_max_opp_pct": _arm["fade_short_max_opp_pct"],
            "fade_short_window_min": _arm["fade_short_window_min"],
        })
        logger.info(
            f"[EntryConfirm] ⏳ {inst.get('symbol')} {_arm['direction']} arm "
            f"锚=${anchor:.2f} mid_arm={_arm['mid_arm']} → {int(cfg['delay_sec'])}s 后复核 "
            f"(RFC-141 §5.10 ruler={_arm['ruler']} grace={_arm['grace_votes']}/{_arm['grace_sweeps']})")
        return "defer"

    # 其他状态 (ABANDONED/EXPIRED/HOLDOUT/HEALED 重入) ⇒ 照常执行, 不重复 arm
    return "execute"


def _restore_arm(inst: Dict[str, Any], state: Dict[str, Any], now_et: datetime,
                 cfg: Dict[str, Any], reprice_params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """重启后按持久化旗标重建 pending 快照; 窗口已整体过期/不可信 ⇒ None。

    只续上 arm 时冻结的窗口 (armed_at/anchor/direction/delay/ruler), 不用当前
    市价重造锚 — 重建失败才走 HEALED 放行, 重启不得把确认门降级为立即执行。
    grace 参数未落旗标 (向后兼容旧行), 用 cfg 现值; 窗口内配置通常未变。
    """
    try:
        inst_id = int(inst.get("id") or 0)
        anchor = float(state.get("anchor") or 0)
        direction = str(state.get("direction") or "")
        armed_raw = state.get("armed_at")
        if inst_id <= 0 or anchor <= 0 or direction not in ("LONG", "SHORT") or not armed_raw:
            return None
        armed_at = datetime.fromisoformat(str(armed_raw))
        if armed_at.tzinfo is None:
            # 旧行可能无时区 (naive) ⇒ 按调用方 now_et 时区解释, 避免 aware/naive 相减报错
            armed_at = armed_at.replace(tzinfo=now_et.tzinfo)
        _ttl = state.get("ttl_sec")
        ttl = int(DEFAULT_TTL_SEC if _ttl is None else _ttl)
        expire_at = armed_at + timedelta(seconds=ttl)
        if now_et > expire_at:
            return None
        _delay = state.get("delay_sec")
        delay_sec = int(DEFAULT_DELAY_SEC if _delay is None else _delay)
        _planned = state.get("planned_entry")
        if _planned is None:
            _planned = _planned_entry(inst)
        _gap = state.get("plan_gap_pct")
        if _gap is None:
            _gap = _plan_gap_pct(anchor, _planned)
        _gap_shadow_enabled = state.get("plan_gap_shadow_enabled")
        if _gap_shadow_enabled is None:
            _gap_shadow_enabled = bool(cfg.get("plan_gap_shadow_enabled", True))
        _gap_shadow_pct = state.get("plan_gap_shadow_pct")
        if _gap_shadow_pct is None:
            _gap_shadow_pct = float(cfg.get("plan_gap_shadow_pct", 1.0) or 1.0)
        _supply = state.get("supply_selection")
        if not isinstance(_supply, dict):
            _supply = _supply_selection(inst)
        return {
            "inst_id": inst_id,
            "symbol": inst.get("symbol"),
            "decision_id": inst.get("decision_id"),
            "direction": direction,
            "anchor": anchor,
            "armed_at": armed_at,
            "expire_at": expire_at,
            "notional": _notional(inst, reprice_params),
            "session_min": _session_min(armed_at),
            "delay_sec": delay_sec,
            "ruler": str(state.get("ruler") or cfg.get("ruler") or DEFAULT_RULER),
            "grace_sweeps": int(cfg.get("grace_sweeps") or 0),
            "grace_votes": int(cfg.get("grace_votes") or DEFAULT_GRACE_VOTES),
            # RFC-153: 旧旗标缺这些键时回落当前 cfg (不把在途单静默降级, 也不伪造历史值)
            "entry_archetype": str(state.get("entry_archetype") or ""),
            "supply_selection": dict(_supply),
            "planned_entry": _planned,
            "plan_gap_pct": _gap,
            "plan_gap_shadow_enabled": bool(_gap_shadow_enabled),
            "plan_gap_shadow_pct": float(_gap_shadow_pct),
            "fade_short_chase_cap_enabled": bool(
                state.get("fade_short_chase_cap_enabled")
                if state.get("fade_short_chase_cap_enabled") is not None
                else cfg.get("fade_short_chase_cap_enabled", False)),
            "fade_short_max_opp_pct": float(
                state.get("fade_short_max_opp_pct")
                if state.get("fade_short_max_opp_pct") is not None
                else (cfg.get("fade_short_max_opp_pct") or 0.0)),
            "fade_short_window_min": int(
                state.get("fade_short_window_min")
                if state.get("fade_short_window_min") is not None
                else (cfg.get("fade_short_window_min") or 0)),
            "mid_arm": state.get("mid_arm"),
            "_path_shadow_samples": [],
            "path_shadow_restored": True,
            "restored": True,
        }
    except Exception as _e:
        logger.warning(f"[EntryConfirm] 孤儿重建失败 (转 HEALED 放行): {_e}", exc_info=True)
        return None


def defer_pending(inst: Dict[str, Any], now_et: datetime) -> bool:
    """reprice **之前**调用: True ⇒ 该指令本轮跳过 (pending 中, 不 reprice 防锚漂;
    或已终态 ABANDONED/EXPIRED, 不再执行)。

    终态拦截靠 metadata 旗标 (跨重启有效), 活跃 pending 拦截靠内存表;
    PENDING 孤儿必须放行到 gate_at_execute 自愈, 不得在此永久拦截。
    """
    state = read_state(inst)
    st = str(state.get("status") or "")
    if st in (ST_ABANDONED, ST_EXPIRED):
        return True
    if st == ST_PENDING and int(inst.get("id") or 0) in _PENDING:
        return True
    return False


def _notional(inst: Dict[str, Any], reprice_params: Optional[Dict[str, Any]]) -> float:
    try:
        _e = float((reprice_params or {}).get("entry_price") or inst.get("entry_price") or 0)
        _s = float((reprice_params or {}).get("position_size") or inst.get("position_size") or 0)
        return round(_e * _s, 2) if _e > 0 and _s > 0 else 0.0
    except Exception:
        return 0.0


def _session_min(now_et: datetime) -> Optional[int]:
    """距 09:30 ET 的分钟数 (供 R1 分层: SHORT×开盘窗 = 0..15)。失败 None。"""
    try:
        open_ = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return int((now_et - open_).total_seconds() // 60)
    except Exception:
        return None


def _evict_expired(now_et: datetime) -> None:
    for _k in [k for k, v in _PENDING.items() if v.get("expire_at") and v["expire_at"] < now_et]:
        _PENDING.pop(_k, None)


# ══════════════════════════════════════════════════════════════════
# 快循环侧: sweep 判定
# ══════════════════════════════════════════════════════════════════
def sweep_entry_confirmations(scheduler, db, now_et: datetime) -> Dict[str, int]:
    """10s 快循环调用: 对到点 (arm+delay_sec) 的 pending 指令做确认判定。

    返回计数 {'confirmed': n, 'abandoned': n, 'expired': n, 'pending': n}。
    任何单笔异常只影响该笔 (逐笔隔离), 不冒泡到快循环。
    v2: 先处理弃单后观测补记 (即使无 pending 也要跑 — scheduler 只在
    pending_count()>0 时调本函数, 而 pending_count 已含 watch, 见该函数注释)。
    """
    out = {"confirmed": 0, "abandoned": 0, "expired": 0, "pending": 0}
    try:
        _sweep_post_watch(scheduler, now_et)
    except Exception as _e:
        # 观测面异常绝不影响判定面 (纯留痕, 失败只 warn)
        logger.warning(f"[EntryConfirm] 弃单后补记异常 (不影响判定): {_e}", exc_info=True)
    if not _PENDING:
        return out

    for inst_id, arm in list(_PENDING.items()):
        for offset in _PATH_SHADOW_OFFSETS_SEC[:-1]:
            try:
                _record_path_sample(scheduler, arm, offset, now_et)
            except Exception as _e:
                logger.warning(
                    f"[EntryConfirm] {arm.get('symbol')} 60/90s 路径影子异常 "
                    f"(不影响判定): {_e}", exc_info=True)

    due = [(k, v) for k, v in list(_PENDING.items())
           if (now_et - v["armed_at"]).total_seconds() >= v["delay_sec"]]
    for inst_id, arm in due:
        try:
            _judge_one(scheduler, db, inst_id, arm, now_et, out)
        except Exception as _e:
            # 逐笔隔离: 单笔判定失败 → 弹出 pending 让 auto_execute 孤儿自愈放行
            logger.warning(
                f"[EntryConfirm] {arm.get('symbol')} 判定异常 → 弹出 pending "
                f"(下轮 auto_execute 自愈放行): {_e}", exc_info=True)
            _PENDING.pop(inst_id, None)
    out["pending"] = len(_PENDING)
    return out


def _evaluate(direction: str, anchor: float, mid_arm, ruler: str, q: Dict[str, Any]):
    """v2 判定核心: 返回 (still_triggers, opp_cost_pct, mid_move_pct, ruler_used)。

    - opp_cost_pct 恒 = 执行锚×last (eval_confirm 原公式) — v1 连续口径, 判据③同尺;
    - ruler=mid 且两端 mid 有效 ⇒ mid-mid 判定 (still/mid_move 来自 eval_confirm 同式,
      方向归一: 正 = 顺交易方向); 任一端缺 ⇒ 回落 legacy 混尺 (锚×last) + ruler_used 留痕,
      裁决时可分桶剔除。
    """
    last = float(q["last"])
    still_legacy, opp = eval_confirm(direction, float(anchor), last)
    if ruler == "mid" and mid_arm and q.get("mid"):
        still_mid, mid_move = eval_confirm(direction, float(mid_arm), float(q["mid"]))
        return still_mid, round(opp, 4), round(mid_move, 4), "mid"
    return still_legacy, round(opp, 4), None, "legacy"


def _record_path_sample(scheduler, arm: Dict[str, Any], target_offset_sec: int,
                        now_et: datetime, q: Optional[Dict[str, Any]] = None) -> None:
    """记录 60/90/120s mid 路径；只写内存，绝不参与 live/grace 判定。"""
    if int(arm.get("delay_sec") or 0) != _PATH_SHADOW_DELAY_SEC:
        return
    samples = arm.setdefault("_path_shadow_samples", [])
    if any(s["target_offset_sec"] == target_offset_sec for s in samples):
        return
    elapsed = (now_et - arm["armed_at"]).total_seconds()
    if elapsed < target_offset_sec:
        return
    quote = q or _current_quotes(scheduler, arm.get("symbol"))
    if quote is None:
        return
    still, opp, mid_move, ruler_used = _evaluate(
        arm["direction"], arm["anchor"], arm.get("mid_arm"),
        str(arm.get("ruler") or DEFAULT_RULER), quote)
    samples.append({
        "target_offset_sec": target_offset_sec,
        "elapsed_sec": int(elapsed),
        "timing_error_sec": int(elapsed - target_offset_sec),
        "still_triggers": bool(still),
        "opp_cost_pct": opp,
        "mid_move_pct": mid_move,
        "ruler_used": ruler_used,
        "last": quote.get("last"), "bid": quote.get("bid"), "ask": quote.get("ask"),
        "mid": quote.get("mid"), "quote_ts": quote.get("ts"),
        "book_status": quote.get("book_status"),
    })


def _path_shadow_summary(arm: Dict[str, Any], live_still: bool) -> Dict[str, Any]:
    """R1 影子：60/90/120 三个有效 mid 观察，真正 >=2/3 同侧才确认。"""
    samples = list(arm.get("_path_shadow_samples") or [])
    by_offset = {s["target_offset_sec"]: s for s in samples}
    reasons = []
    for offset in _PATH_SHADOW_OFFSETS_SEC:
        sample = by_offset.get(offset)
        if sample is None:
            reasons.append(f"missing_{offset}s")
        elif sample.get("ruler_used") != "mid" or sample.get("mid_move_pct") is None:
            reasons.append(f"non_mid_{offset}s")
        elif abs(int(sample.get("timing_error_sec") or 0)) > _PATH_SHADOW_MAX_TIMING_ERROR_SEC:
            reasons.append(f"timing_{offset}s")
    evaluable = not reasons
    votes = [1 if by_offset[o]["still_triggers"] else 0
             for o in _PATH_SHADOW_OFFSETS_SEC if o in by_offset]
    r1_would_confirm = (sum(votes) >= 2) if evaluable else None
    return {
        "schema": "entry_confirm_path_shadow_v1",
        "rule": "R1 fixed 60/90/120s; >=2 of 3 valid mid same-side votes; no grace; record-only",
        "units": "elapsed/timing=seconds; mid_move_pct=direction-normalized percent",
        "samples": samples,
        "evaluable": evaluable,
        "unevaluable_reasons": reasons,
        "r1_votes": votes,
        "r1_would_confirm": r1_would_confirm,
        "r1_agrees_with_live": (r1_would_confirm == bool(live_still)) if evaluable else None,
    }


def _path_shadow_summary_safe(arm: Dict[str, Any], live_still: bool) -> Dict[str, Any]:
    try:
        return _path_shadow_summary(arm, live_still)
    except Exception as _e:
        return {"schema": "entry_confirm_path_shadow_v1", "evaluable": False,
                "unevaluable_reasons": ["observer_error"], "error": str(_e),
                "r1_would_confirm": None, "r1_agrees_with_live": None}


def _judge_one(scheduler, db, inst_id: int, arm: Dict[str, Any],
               now_et: datetime, out: Dict[str, int]) -> None:
    symbol = arm.get("symbol")

    # 超期 (市场关闭/取价长期失败) ⇒ EXPIRED, 不计入样本
    if arm.get("expire_at") and now_et > arm["expire_at"]:
        _PENDING.pop(inst_id, None)
        _write_state(db, inst_id, {"status": ST_EXPIRED,
                                   "armed_at": arm["armed_at"].isoformat(),
                                   "reason": "ttl"})
        out["expired"] += 1
        # 2026-11-05: 超期也落一行前瞻 (status="expired") —— 此前只在日志里出现,
        #   结算器按 status=="ok" 过滤 ⇒ 这些笔在"assigned / resolved / unresolved"
        #   三栏里凭空消失, 覆盖率看起来比实际好。record-only, 不改判定。
        _write_forward_row({
            "type": "entry_confirm", "schema": SCHEMA_V2,
            "ts": now_et.isoformat(), "date": now_et.strftime("%Y-%m-%d"),
            "inst_id": inst_id, "decision_id": arm.get("decision_id"),
            "symbol": symbol, "direction": arm.get("direction"),
            "arm": "treatment", "status": "expired", "outcome": ST_EXPIRED,
            "still_triggers": None, "opp_cost_pct": None,
            "anchor": float(arm.get("anchor") or 0.0),
            "planned_entry": arm.get("planned_entry"),
            "plan_gap_pct": arm.get("plan_gap_pct"),
            "entry_archetype": arm.get("entry_archetype"),
            "supply_selection": arm.get("supply_selection") or {},
            "notional": arm.get("notional") or 0.0,
            "session_min": arm.get("session_min"),
            "armed_at": arm["armed_at"].isoformat(),
            "expired_at": now_et.isoformat(),
            "expire_reason": "ttl",
            "delay_sec": arm.get("delay_sec"),
            "delay_actual_sec": int((now_et - arm["armed_at"]).total_seconds()),
        })
        logger.warning(f"[EntryConfirm] {symbol} pending 超期 → EXPIRED (不计样本)")
        return

    q = _current_quotes(scheduler, symbol)
    if q is None:
        # 取不到价 ⇒ 留在 pending 下轮再试 (到 TTL 才 EXPIRED)。
        # grace 计数同样不消耗: 无价格 ≠ 逆向证据 (否则断价 30s 会白送 2 票逆向)。
        return

    still, opp, mid_move, ruler_used = _evaluate(
        arm["direction"], arm["anchor"], arm.get("mid_arm"),
        str(arm.get("ruler") or DEFAULT_RULER), q)
    try:
        _record_path_sample(scheduler, arm, _PATH_SHADOW_OFFSETS_SEC[-1], now_et, q)
    except Exception as _e:
        logger.warning(f"[EntryConfirm] {symbol} 120s 路径影子异常 (不影响判定): {_e}",
                       exc_info=True)

    # ── RFC-153: FADE_SHORT 开盘快跌禁追 ──
    #   本意是等反抽再空; 确认窗内价格已顺向走掉 ≥1% 时, "确认"其实是追空。
    #   只作用于 FADE_SHORT + 开盘窗口; 到点直接放弃, 不进 grace (不是单点噪声)。
    _arch = str(arm.get("entry_archetype") or "")
    _cap_enabled = bool(arm.get("fade_short_chase_cap_enabled", False))
    _cap_max = float(arm.get("fade_short_max_opp_pct") or 0.0)
    _cap_window = int(arm.get("fade_short_window_min") or 0)
    _arm_session = arm.get("session_min")
    _fs_cap_hit = bool(
        _cap_enabled and _arch == "FADE_SHORT" and _cap_max > 0
        and isinstance(_arm_session, int) and 0 <= _arm_session < _cap_window
        and still and opp >= _cap_max
    )

    # ── v2 grace 持续性 (RFC-141 §5.10): 首见逆向不即弃, ≤grace_sweeps 次内
    #    ≥grace_votes 次逆向才弃; 任一同侧即确认; 耗尽仍不足票 ⇒ 确认 (fail toward trade) ──
    grace = arm.get("grace")
    grace_outcome = None
    if _fs_cap_hit:
        grace_outcome = "fade_short_chase_cap"
        still = False
    elif not still and int(arm.get("grace_sweeps") or 0) > 0:
        if grace is None:
            arm["grace"] = {"left": int(arm["grace_sweeps"]), "adv": 0}
            logger.info(
                f"[EntryConfirm] ⚠️ {symbol} {arm['direction']} 到点首见逆向 "
                f"(ruler={ruler_used} mid_move={mid_move} opp={opp:+.3f}%) → grace 复核: "
                f"{arm['grace_sweeps']} 次内 ≥{arm['grace_votes']} 次逆向才弃 (v2)")
            return
        grace["adv"] += 1
        grace["left"] -= 1
        if grace["adv"] >= int(arm.get("grace_votes") or DEFAULT_GRACE_VOTES):
            grace_outcome = "persisted"        # 反转持续 ⇒ 弃 (走下方 ABANDONED)
        elif grace["left"] <= 0:
            grace_outcome = "unpersisted"      # 反转未持续 ⇒ 确认放行
            still = True
        else:
            return                             # 票数不足且次数未尽 ⇒ 等下一 sweep
    elif still and grace is not None:
        grace_outcome = "recovered"            # grace 期内回正 ⇒ 确认 (v2 救回噪声杀的路径)

    _PENDING.pop(inst_id, None)
    _grace_trace = (None if grace is None else {
        "sweeps_used": int(arm.get("grace_sweeps") or 0) - int(grace["left"]),
        "adverse_votes": grace["adv"], "outcome": grace_outcome})
    _delay_actual = int((now_et - arm["armed_at"]).total_seconds())
    _quote_fields = {"mid_arm": arm.get("mid_arm"),
                     "mid_at_confirm": q.get("mid"),
                     "mid_move_pct": mid_move, "ruler_used": ruler_used,
                     "bid_at_confirm": q.get("bid"), "ask_at_confirm": q.get("ask"),
                     "quote_ts": q.get("ts")}
    _ab_cause = (None if still
                 else ("fade_short_chase_cap" if _fs_cap_hit else "reversed"))
    # [RFC-170] 追价影子 (record-only): 只标记, 不改 still/abandon 判定
    _chase_shadow = bool(still and chase_shadow_flag(opp))

    if still:
        # 确认通过: 清 pending 旗标 → 下轮 auto_execute 正常 reprice + 下单
        _write_state(db, inst_id, {
            "status": ST_CONFIRMED, "armed_at": arm["armed_at"].isoformat(),
            # [DEB-20261106 P2c] 方向必须随终态一起重写: merge_instruction_metadata 是
            #   **顶层浅合并** ⇒ patch 里的 entry_confirm 会整块替换旧对象, PENDING 时写入的
            #   direction 会被静默抹掉 (2026-11-06 实测: 80 行弃单里 48 行无方向, 结算器只能吃到 31 行)。
            "direction": arm.get("direction"), "original_direction": arm.get("direction"),
            "confirmed_at": now_et.isoformat(), "anchor": arm["anchor"],
            "planned_entry": arm.get("planned_entry"),
            "plan_gap_pct": arm.get("plan_gap_pct"),
            "px_at_confirm": round(float(q["last"]), 4), "opp_cost_pct": opp,
            "delay_actual_sec": _delay_actual, **_quote_fields, "grace": _grace_trace,
        })
        out["confirmed"] += 1
        logger.info(
            f"[EntryConfirm] ✅ {symbol} 确认通过 (锚${arm['anchor']:.2f} "
            f"现价${q['last']:.2f} opp={opp:+.3f}% ruler={ruler_used}"
            f"{' grace=' + str(_grace_trace) if _grace_trace else ''}) → 下轮执行")
        if _chase_shadow:
            # [RFC-170] 让追价成本在实时日志里可见 (等 n 到位再裁, 不拦)
            logger.info(
                f"[EntryConfirm·追价影子] {symbol} {arm['direction']} 确认时追价 "
                f"opp={opp:+.3f}% ≥ {CHASE_SHADOW_PCT:.2f}% → would_abandon "
                f"(record-only, 不拦)")
    else:
        # 放弃: 终态旗标 + 复用既有 guardrail SKIP 语义落库。
        #   为什么不只写 metadata 旗标: defer_pending 已能拦住 (双保险), 但库里该指令
        #   仍是「活跃待执行」方向且永不执行 ⇒ 复盘/app 侧分不清「没触发」与「被门放弃」,
        #   误导性比看不出来更糟。复用 mark_instruction_guardrail_skip (WHERE 带
        #   executed=0 AND execution_state='PENDING' ⇒ 不碰已成交/在途行) 后:
        #     · dashboard「已跳过」区 reason 可见 (renderSkipItem 读 skip_reason, 既有 UX)
        #     · auto_execute 循环内 direction=='SKIP' 分支拦下 (auto_execute_flow.py:160,
        #       与函数 docstring 契约「direction != SKIP 才执行」一致; 注意取数
        #       get_instructions_by_date 本身不过滤 SKIP, 靠循环内该分支)
        #     · 与其它 guardrail 拦截 (组合/NOTIONAL_TOO_SMALL) 库里表现一致
        _ab_ok = False
        try:
            _ab_ok = bool(db.mark_instruction_guardrail_skip(
                inst_id, SKIP_REASON_ABANDONED))
        except Exception as _e:
            logger.warning(
                f"[EntryConfirm] {symbol} ABANDONED 落 SKIP 失败 (旗标仍拦): {_e}",
                exc_info=True)
        if not _ab_ok:
            # 行已离开 PENDING (执行竞态) ⇒ 不动执行状态, 只留旗标兜底 (defer_pending 拦)
            logger.info(
                f"[EntryConfirm] {symbol} ABANDONED 未落 SKIP (行已离开 PENDING, "
                f"竞态保护) — 旗标仍终态拦截")
        _write_state(db, inst_id, {
            "status": ST_ABANDONED, "armed_at": arm["armed_at"].isoformat(),
            # [DEB-20261106 P2c] 同 CONFIRMED: 顶层浅合并会整块替换 entry_confirm ⇒ 不重写就丢方向
            "direction": arm.get("direction"), "original_direction": arm.get("direction"),
            "abandoned_at": now_et.isoformat(), "anchor": arm["anchor"],
            "px_at_confirm": round(float(q["last"]), 4), "opp_cost_pct": opp,
            "delay_actual_sec": _delay_actual, **_quote_fields, "grace": _grace_trace,
            "skip_marked": _ab_ok,
            "abandon_cause": _ab_cause,
            "entry_archetype": _arch,
            "planned_entry": arm.get("planned_entry"),
            "plan_gap_pct": arm.get("plan_gap_pct"),
            "chase_cap_hit": _fs_cap_hit,
            "chase_cap_threshold_pct": (_cap_max if _fs_cap_hit else None),
        })
        out["abandoned"] += 1
        logger.info(
            f"[EntryConfirm] ⛔ {symbol} {arm['direction']} "
            f"{'FADE_SHORT 开盘快跌禁追' if _fs_cap_hit else '反转持续确认'} "
            f"(锚${arm['anchor']:.2f} 现价${q['last']:.2f} ruler={ruler_used} "
            f"opp={opp:+.3f}% arch={_arch or '-'} session_min={_arm_session} "
            f"cap={_cap_max if _fs_cap_hit else '-'}% grace={_grace_trace}) → 放弃入场")
        # v2: 弃单后观测 (+5m/+10m 补记, 纯留痕供「真避免 vs 误杀」分解)
        if len(_POST_WATCH) >= _POST_WATCH_MAX:
            _oldest = min(_POST_WATCH, key=lambda k: _POST_WATCH[k]["abandoned_at"])
            _POST_WATCH.pop(_oldest, None)
        _POST_WATCH[inst_id] = {
            "symbol": symbol, "direction": arm["direction"],
            "anchor": float(arm["anchor"]), "mid_arm": arm.get("mid_arm"),
            "px_at_confirm": float(q["last"]), "mid_at_confirm": q.get("mid"),
            "decision_id": arm.get("decision_id"),
            "supply_selection": arm.get("supply_selection") or {},
            "abandoned_at": now_et, "m5": False, "m10": False}

    # 前瞻样本行 (两臂都落, 供结算器聚合); schema v2 = v1 全字段 + 判尺/grace/双边报价
    _write_forward_row({
        "type": "entry_confirm", "schema": SCHEMA_V2,
        "ts": now_et.isoformat(), "date": now_et.strftime("%Y-%m-%d"),
        "inst_id": inst_id, "decision_id": arm.get("decision_id"),
        "symbol": symbol, "direction": arm["direction"], "arm": "treatment",
        "status": "ok",
        "still_triggers": bool(still),
        "opp_cost_pct": opp,
        "anchor": float(arm["anchor"]),
        "px_at_confirm": round(float(q["last"]), 4),
        "delay_actual_sec": _delay_actual,
        "notional": arm.get("notional") or 0.0,
        "session_min": arm.get("session_min"),
        "outcome": ST_CONFIRMED if still else ST_ABANDONED,
        "entry_archetype": _arch,
        "supply_selection": arm.get("supply_selection") or {},
        "planned_entry": arm.get("planned_entry"),
        "plan_gap_pct": arm.get("plan_gap_pct"),
        "plan_gap_shadow": _plan_gap_shadow(_arch, arm.get("plan_gap_pct"), {
            "plan_gap_shadow_enabled": arm.get("plan_gap_shadow_enabled", True),
            "plan_gap_shadow_pct": arm.get("plan_gap_shadow_pct", 1.0),
        }),
        "plan_gap_threshold_pct": float(arm.get("plan_gap_shadow_pct", 1.0) or 1.0),
        "abandon_cause": _ab_cause,
        "chase_cap_hit": _fs_cap_hit,
        "chase_cap_threshold_pct": (_cap_max if _fs_cap_hit else None),
        # [RFC-170] 追价影子 (record-only): 结算器按该字段切「门全量 vs 门+追价上界」
        "chase_shadow_pct": CHASE_SHADOW_PCT,
        "chase_shadow_would_abandon": _chase_shadow,
        **_quote_fields, "grace": _grace_trace,
        "path_shadow": _path_shadow_summary_safe(arm, still),
    })


def _sweep_post_watch(scheduler, now_et: datetime) -> None:
    """v2 弃单后观测: ABANDONED 后 +5m/+10m 各补记一行 (type=entry_confirm_post)。

    纯观测 — 不进 _agg、不参与任何判定。读法 (裁决日分解用):
      mid_move_pct 方向归一 (正=顺原交易方向) ⇒ 弃单后仍深负 = 真避免;
      回正/转正 = 误杀赢家。取价失败不记标记, 下轮再试; 超 _POST_GIVEUP_SEC 弃记。
    """
    for _iid, _w in list(_POST_WATCH.items()):
        _age = (now_et - _w["abandoned_at"]).total_seconds()
        if _age > _POST_GIVEUP_SEC:
            logger.info(f"[EntryConfirm] {_w.get('symbol')} 弃单后补记超 {_POST_GIVEUP_SEC}s "
                        f"未齐 (m5={_w['m5']} m10={_w['m10']}) → 弃记")
            _POST_WATCH.pop(_iid, None)
            continue
        for _key, _sec, _label in _POST_MARKS:
            if _w.get(_key) or _age < _sec:
                continue
            _q = _current_quotes(scheduler, _w["symbol"])
            if _q is None:
                break                # 取价失败: 不消耗标记, 下轮再试
            _, _last_move = eval_confirm(_w["direction"], float(_w["anchor"]), float(_q["last"]))
            _mid_move = None
            if _w.get("mid_arm") and _q.get("mid"):
                _, _mm = eval_confirm(_w["direction"], float(_w["mid_arm"]), float(_q["mid"]))
                _mid_move = round(_mm, 4)
            _, _last_from_confirm = eval_confirm(
                _w["direction"], float(_w["px_at_confirm"]), float(_q["last"]))
            _mid_from_confirm = None
            if _w.get("mid_at_confirm") and _q.get("mid"):
                _, _mm_confirm = eval_confirm(
                    _w["direction"], float(_w["mid_at_confirm"]), float(_q["mid"]))
                _mid_from_confirm = round(_mm_confirm, 4)
            _write_forward_row({
                "type": "entry_confirm_post", "schema": SCHEMA_V2,
                "ts": now_et.isoformat(), "date": now_et.strftime("%Y-%m-%d"),
                "inst_id": _iid, "decision_id": _w.get("decision_id"),
                "symbol": _w["symbol"], "direction": _w["direction"],
                "supply_selection": _w.get("supply_selection") or {},
                "horizon": _label, "anchor": float(_w["anchor"]),
                "mid_arm": _w.get("mid_arm"),
                "px_last": round(float(_q["last"]), 4),
                "bid": _q.get("bid"), "ask": _q.get("ask"), "mid": _q.get("mid"),
                "last_move_pct": round(_last_move, 4), "mid_move_pct": _mid_move,
                # 方向归一百分比：正=弃单后顺原交易方向；用于 M4 时取 max(0, -pct)。
                "last_from_confirm_pct": round(_last_from_confirm, 4),
                "mid_from_confirm_pct": _mid_from_confirm,
                "px_at_confirm": float(_w["px_at_confirm"]),
                "mid_at_confirm": _w.get("mid_at_confirm"),
                "quote_ts": _q.get("ts"),
            })
            _w[_key] = True
        if _w.get("m5") and _w.get("m10"):
            _POST_WATCH.pop(_iid, None)


def _current_quotes(scheduler, symbol: str) -> Optional[Dict[str, Any]]:
    """取现价快照 {last, bid, ask, mid, ts} (Futu push 缓存优先, 与退出快循环同源, 近零成本)。

    last 缺失/非正 ⇒ None (与 v1 _current_price 同语义, 调用方留 pending 下轮再试)。
    bid/ask 缺失/交叉/盘口为 last 兜底代理 ⇒ mid=None (v2 判定该笔回落
    legacy 混尺, ruler_used 留痕)。
    """
    try:
        _dr = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not _dr:
            return None
        _ctx = _dr.get_full_market_context(symbol)
        if not _ctx:
            return None
        _p = _ctx.get("price")
        if _p is None or float(_p) <= 0:
            return None
        bid = ask = mid = None
        book_status = "missing"
        _book_is_proxy = (
            _ctx.get("book_is_proxy") is True
            or _ctx.get("lv3_book_is_proxy") is True
        )
        try:
            _b = float(_ctx.get("lv3_bid") or _ctx.get("bid") or 0)
            _a = float(_ctx.get("lv3_ask") or _ctx.get("ask") or 0)
            if _book_is_proxy:
                book_status = "proxy"
            elif _b <= 0 or _a <= 0:
                book_status = "missing"
            elif _b > _a:
                book_status = "crossed"
            else:
                bid, ask, mid = _b, _a, round((_b + _a) / 2, 6)
                book_status = "ok"
        except (TypeError, ValueError):
            bid = ask = mid = None
            book_status = "invalid"
        return {"last": float(_p), "bid": bid, "ask": ask, "mid": mid,
                "ts": _ctx.get("timestamp"), "book_status": book_status}
    except Exception:
        return None


def pending_count() -> int:
    """供 dashboard/健康检查读当前工作项数 = pending + 弃单后观测 watch。

    ⚠️ watch 必须计入: scheduler 快循环只在 pending_count()>0 时才调 sweep
    (scheduler_core.py), 若不含 watch ⇒ 全部 pending 判完后 +5m/+10m 补记永远不落。
    """
    return len(_PENDING) + len(_POST_WATCH)
