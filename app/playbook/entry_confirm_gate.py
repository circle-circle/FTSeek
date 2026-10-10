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
富途 app 上的真实 SIM 盈亏。2026-10-10 用户裁「直接走 sim 交易链路, 不用影子
攒样看 sim 交易结果」⇒ `holdout_pct=0` 全量走门, 不留对照臂 (留了会稀释 SIM
结果); 对照臂机制 inert 保留 (改配置值即恢复), 代价=无同期对照、受市况混淆。

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
   metadata 旗标若残留 PENDING_CONFIRM 而内存无对应项 ⇒ 视为过期, 清旗标
   恢复正常执行。绝不因重启把指令永久卡死 (失败路径纪律)。

6. **fail-open 到「照常执行」**: 本门任何异常 ⇒ 放行原路径, 不下单不是
   安全方向 (会静默减少样本 + 改变基线)。与 chase_cap 门的 fail-close 相反,
   因为那个门拦的是危险单, 这个门拦的是普通单。

判据 (预注册于 docs/decisions/RFC-141 §4, 读数前写死, 裁决日不挪):
  裁决源 = **开门后 SIM 真实盈亏 vs 门前预注册基线** (2026-10-10 用户裁改口径,
  jsonl 降级为机制留痕, 不独立裁决)。样本门 n≥60 ∨ ≥10 交易日。
  ①病灶族收敛: SL_INITIAL 族净均 ≥−$40/笔 (基线 −$157.9069, 收敛≥75%) 或族 n≤1
  ②开盘窗改善: 09:35–09:45 净均 >−$4.1115 ∧ 日聚类 t >−0.3218
  ③机制量: 避免率 ≥30% ∧ CONFIRMED opp_cost <0.3%
  ④不伤赢家: 非 SL_INITIAL 族净均 ≥+$2.79 (基线 +$3.4852 的 80%)
  R1 分层 (SHORT×09:35-09:45) / LONG×开盘窗 只印不判。
  ⚠️ 基线数字由 scripts/settle_entry_confirm_forward.BASELINE 冻结, t 用
  statistics.stdev (ddof=1) 与 _day_clustered_t 同尺; 改动须先改 RFC 预注册文本。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 常量 (配置可覆盖, 见 confirm_gate_config) ──────────────────────────
DEFAULT_DELAY_SEC = 120          # 影子裁过的腿: DELAYS_MIN=(2,5) 中的 2min
DEFAULT_HOLDOUT_PCT = 20         # 对照臂比例 — ⚠️ 仅「缺键回落」默认, 生产已置 0
                                 # (2026-10-10 用户裁全量走门, 见 config 注释与 RFC §2)
DEFAULT_TTL_SEC = 600            # pending 超期即弃 (防跨日/卡死)
_STATE_KEY = "entry_confirm"     # metadata_json 下的旗标键

# 内存 pending 表: inst_id -> arm 快照。有界 (TTL 清理 + 上限防泄漏)。
_PENDING: Dict[int, Dict[str, Any]] = {}
_PENDING_MAX = 200

# 产物 (锚定模块路径 — 相对路径影子污染教训, 2026-09-21)
_ROOT = Path(__file__).resolve().parents[2]
_FORWARD_PATH = _ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"

# 状态枚举 (写进 metadata + jsonl, 供 app/结算器读)
ST_PENDING = "PENDING_CONFIRM"
ST_CONFIRMED = "CONFIRMED"
ST_ABANDONED = "ABANDONED"
ST_EXPIRED = "EXPIRED"
ST_HOLDOUT = "HOLDOUT"

# 放弃单落库用的 skip_reason (进 dashboard「已跳过」区, 与 guardrail 拦截同表现)
SKIP_REASON_ABANDONED = "entry_confirm_reversed"


# ══════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════
def confirm_gate_config(pb_cfg=None) -> Dict[str, Any]:
    """读 playbook.execution_parameters.entry_confirm_* (扁平键, PlaybookConfig 拍平面)。

    pb_cfg 非空时走调用方注入的配置对象 (与 auto_execute_flow 内其它门同源同接缝,
    测试可密封); 否则自建 PlaybookConfig (sweep 侧/独立调用)。

    读不到 ⇒ enabled=False (保守关闭, 维持现状基线, 不静默改交易行为)。
    ⚠️ 键路径 = PlaybookConfig._load 拍平的 execution_parameters 节, 与
    entry_open_blackout_enabled 同源; 不可用 get_dotted("playbook.entry_confirm.*")
    (那是嵌套路径, 拍平面读不到 = 装饰性配置)。
    """
    cfg = {
        "enabled": False,
        "delay_sec": DEFAULT_DELAY_SEC,
        "holdout_pct": DEFAULT_HOLDOUT_PCT,
        "ttl_sec": DEFAULT_TTL_SEC,
    }

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

    try:
        if pb_cfg is None:
            from app.playbook.config import PlaybookConfig
            pb_cfg = PlaybookConfig()
        cfg["enabled"] = bool(pb_cfg.get("entry_confirm_enabled", False))
        cfg["delay_sec"] = _int_or("entry_confirm_delay_sec", DEFAULT_DELAY_SEC)
        cfg["holdout_pct"] = _int_or("entry_confirm_holdout_pct", DEFAULT_HOLDOUT_PCT)
        cfg["ttl_sec"] = _int_or("entry_confirm_ttl_sec", DEFAULT_TTL_SEC)
    except Exception as _e:
        # fail-safe: 配置不可读 ⇒ 门关闭 (维持现状基线, 不静默改交易行为)
        logger.warning(f"[EntryConfirm] 配置读取失败 → 门关闭 (维持现状): {_e}",
                       exc_info=True)
        cfg["enabled"] = False
    return cfg


def _norm_dir(direction: str) -> str:
    """指令方向 (BUY/SELL_SHORT) → 影子口径 (LONG/SHORT)。"""
    d = str(direction or "").upper()
    if d in ("BUY", "LONG"):
        return "LONG"
    if d in ("SELL_SHORT", "SHORT", "SELL"):
        return "SHORT"
    return d or "UNKNOWN"


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
    opp_cost_pct/notional/date), 另加 arm/live 专有字段。写盘失败只 warn。"""
    try:
        _FORWARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_FORWARD_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
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
) -> str:
    """在 auto_execute 即将下单前调用。返回动作:

      'execute'  → 照常下单 (门关闭 / holdout 臂 / 已确认 / 孤儿自愈)
      'defer'    → 本轮不下单 (已 arm, 等 sweep 判定)

    调用方约定: 'defer' 时 skipped_count += 1; continue —— 且本函数必须在
    reprice 之后、execute 之前调用 (需要 reprice 后的执行锚), 而 pending 复检
    (defer_pending) 必须在 reprice **之前** 调用 (防锚漂)。
    """
    try:
        return _gate_inner(inst, db, reprice_params, now_et, cfg)
    except Exception as _e:
        # fail-open 双层防御: 本函数自身任何异常 (分流/取锚/类型) 都不得阻断交易。
        # 拦普通单不是安全方向, 且静默减样 = 改变基线 (与 chase_cap fail-close 相反)。
        logger.warning(f"[EntryConfirm] gate 异常 → 照常执行 (fail-open): {_e}",
                       exc_info=True)
        return "execute"


def _gate_inner(inst, db, reprice_params, now_et, cfg):
    cfg = cfg or confirm_gate_config()
    inst_id = int(inst.get("id") or 0)
    state = read_state(inst)
    st = str(state.get("status") or "")

    # ── 已判定过的终态: CONFIRMED 放行, ABANDONED/EXPIRED 由 defer_pending 拦 ──
    if st == ST_CONFIRMED:
        return "execute"

    # ── 门关闭 ⇒ 零行为变更 ──
    if not cfg.get("enabled") or inst_id <= 0:
        return "execute"

    # ── 孤儿自愈: 旗标说 pending 但内存无 (重启/超期) ⇒ 清旗标照常执行 ──
    if st == ST_PENDING and inst_id not in _PENDING:
        logger.warning(
            f"[EntryConfirm] {inst.get('symbol')} 旗标 PENDING 但内存无记录 "
            f"(重启/超期) → 清旗标照常执行 (孤儿自愈)", exc_info=True)
        _write_state(db, inst_id, {**state, "status": ST_EXPIRED,
                                   "healed_at": now_et.isoformat()})
        return "execute"

    # ── 首次到达执行点: 分流 arm / holdout ──
    if not st:
        if _holdout_bucket(inst_id, int(cfg.get("holdout_pct") or 0)):
            # 对照臂: 照常执行, 只落一行供配对读数
            _write_state(db, inst_id, {"status": ST_HOLDOUT,
                                       "armed_at": now_et.isoformat()})
            _write_forward_row({
                "type": "entry_confirm", "schema": "entry_confirm_forward_v1",
                "ts": now_et.isoformat(), "date": now_et.strftime("%Y-%m-%d"),
                "inst_id": inst_id, "decision_id": inst.get("decision_id"),
                "symbol": inst.get("symbol"),
                "direction": _norm_dir(inst.get("direction")),
                "arm": "holdout", "status": "ok",
                "still_triggers": True, "opp_cost_pct": 0.0,
                "anchor": float((reprice_params or {}).get("entry_price")
                                or inst.get("entry_price") or 0.0),
                "px_at_confirm": None, "notional": _notional(inst, reprice_params),
                "session_min": _session_min(now_et),
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
        }
        if len(_PENDING) >= _PENDING_MAX:
            _evict_expired(now_et)
        _PENDING[inst_id] = _arm
        _write_state(db, inst_id, {
            "status": ST_PENDING, "armed_at": now_et.isoformat(),
            "anchor": anchor, "direction": _arm["direction"],
            "delay_sec": _arm["delay_sec"],
        })
        logger.info(
            f"[EntryConfirm] ⏳ {inst.get('symbol')} {_arm['direction']} arm "
            f"锚=${anchor:.2f} → {int(cfg['delay_sec'])}s 后复核 (RFC-141 前瞻)")
        return "defer"

    # 其他状态 (ABANDONED/EXPIRED/HOLDOUT 重入) ⇒ 照常执行, 不重复 arm
    return "execute"


def defer_pending(inst: Dict[str, Any], now_et: datetime) -> bool:
    """reprice **之前**调用: True ⇒ 该指令本轮跳过 (pending 中, 不 reprice 防锚漂;
    或已终态 ABANDONED/EXPIRED, 不再执行)。

    终态拦截靠 metadata 旗标 (跨重启有效), pending 拦截靠内存表 (快路径)。
    """
    state = read_state(inst)
    st = str(state.get("status") or "")
    if st in (ST_ABANDONED, ST_EXPIRED):
        return True
    if st == ST_PENDING:
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
    """
    out = {"confirmed": 0, "abandoned": 0, "expired": 0, "pending": 0}
    if not _PENDING:
        return out

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
        logger.warning(f"[EntryConfirm] {symbol} pending 超期 → EXPIRED (不计样本)")
        return

    px = _current_price(scheduler, symbol)
    if px is None or px <= 0:
        # 取不到价 ⇒ 留在 pending 下轮再试 (到 TTL 才 EXPIRED)
        return

    still, opp = eval_confirm(arm["direction"], float(arm["anchor"]), float(px))
    _PENDING.pop(inst_id, None)

    if still:
        # 确认通过: 清 pending 旗标 → 下轮 auto_execute 正常 reprice + 下单
        _write_state(db, inst_id, {
            "status": ST_CONFIRMED, "armed_at": arm["armed_at"].isoformat(),
            "confirmed_at": now_et.isoformat(), "anchor": arm["anchor"],
            "px_at_confirm": round(float(px), 4), "opp_cost_pct": round(opp, 4),
            "delay_actual_sec": int((now_et - arm["armed_at"]).total_seconds()),
        })
        out["confirmed"] += 1
        logger.info(
            f"[EntryConfirm] ✅ {symbol} 确认通过 (锚${arm['anchor']:.2f} "
            f"现价${px:.2f} opp={opp:+.3f}%) → 下轮执行")
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
            "abandoned_at": now_et.isoformat(), "anchor": arm["anchor"],
            "px_at_confirm": round(float(px), 4), "opp_cost_pct": round(opp, 4),
            "delay_actual_sec": int((now_et - arm["armed_at"]).total_seconds()),
            "skip_marked": _ab_ok,
        })
        out["abandoned"] += 1
        logger.info(
            f"[EntryConfirm] ⛔ {symbol} {arm['direction']} 2min 内反转 "
            f"(锚${arm['anchor']:.2f} 现价${px:.2f}) → 放弃入场 (D2' 前瞻)")

    # 前瞻样本行 (两臂都落, 供结算器聚合)
    _write_forward_row({
        "type": "entry_confirm", "schema": "entry_confirm_forward_v1",
        "ts": now_et.isoformat(), "date": now_et.strftime("%Y-%m-%d"),
        "inst_id": inst_id, "decision_id": arm.get("decision_id"),
        "symbol": symbol, "direction": arm["direction"], "arm": "treatment",
        "status": "ok",
        "still_triggers": bool(still),
        "opp_cost_pct": round(float(opp), 4),
        "anchor": float(arm["anchor"]),
        "px_at_confirm": round(float(px), 4),
        "delay_actual_sec": int((now_et - arm["armed_at"]).total_seconds()),
        "notional": arm.get("notional") or 0.0,
        "session_min": arm.get("session_min"),
        "outcome": ST_CONFIRMED if still else ST_ABANDONED,
    })


def _current_price(scheduler, symbol: str) -> Optional[float]:
    """取现价 (Futu push 缓存优先, 与退出快循环同源, 近零成本)。失败 None。"""
    try:
        _dr = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not _dr:
            return None
        _ctx = _dr.get_full_market_context(symbol)
        if not _ctx:
            return None
        _p = _ctx.get("price")
        return float(_p) if _p is not None and float(_p) > 0 else None
    except Exception:
        return None


def pending_count() -> int:
    """供 dashboard/健康检查读当前 pending 数。"""
    return len(_PENDING)
