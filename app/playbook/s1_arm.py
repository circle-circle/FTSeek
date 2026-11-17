"""S1 臂闸 (判据字典 B23 / 注册表 s1_short_after_open) — 单一真源。

实验授权 (用户 2026-11-15 裁): **只做空 / 只在 ET 09:45 之后 / 单笔 ≤$4,200 /
单日 ≤2 笔 / 总敞口 ≤$8,400**, 累计净 −$300 或前 15 笔净为负即停 (kill switch)。

纪律:
  · `s1_arm_enabled=false` (默认) 时 `cycle_state()` 返回 enabled=False, 调用方一行都不拦
    ⇒ 零行为变更, 回滚 = 把该键改回 false (热载 ~60s, 无需重启亦可)。
  · 本模块只读 DB (`mode=ro`), 不下单、不写交易库、不改任何指令字段。
  · 帽的判据全部来自判据字典 B23 原文, 改阈值 = 重新裁决, 不得在此处"微调"。
  · **账本按臂别解析 (P8)**: `target_env` 形参一律无默认值 ⇒ 漏传即 TypeError,
    不静默回落 sim。原 `_DB` 写死 sim 研究库, 而真钱空单落 `ftseek_us_live.db`
    ⇒ 签署日敞口帽恒不绑定、kill switch 恒不触发 (RFC-212 §5.1)。
  · **读库失败 fail-close (P9)**: 敞口/kill switch 都判不出来时一律拒绝新开仓,
    不再 `except → 0.0`。风险限额的正确失效方向是"关掉", 不是"放开"。
  · **只约束自己的方向 (P10, 用户 2026-11-16 裁选项 1)**: 非臂方向放行且不施加任何
    S1 约束。原实现 `return False` ⇒ 开闸即把整条 playbook 自动执行流变成只做空,
    与同日起跑的 RFC-209 前瞻 cohort (人群 71% 为多头) 正面相撞。B23 人群判据不受
    影响 (`S1_WHERE` 内 `direction='SHORT'` 仍是过滤条件) ⇒ 不构成重新裁决。
  · **日笔数一条账本一个锚点一个增量 (P13, 用户 2026-11-16 裁「按重设计实施」)**:
    日笔数 = 臂账本今日 SHORT 成交数 (谓词即语义), 不再依赖预算层全流向计数器
    (原实现多头成交吃空头臂额度 ⇒ 实验零样本, DEB-20261116 C7)。轮内只在臂方向
    成交后 += 1 (唯一增量, 兜簿记时滞); 敞口帽维持全方向口径 (已裁保守面, 不动)。
  · **授权值单源 + 缺键 fail-close (C11/C12, DEB-20261116 §10 第 6 步)**: 九键唯一
    真源 = unified_config.yaml, DEFAULTS 双写已删; 缺键/读失败抛 S1ConfigError,
    各消费点转 fail-close (degraded/拒/0/停), 绝不回落第二份数值。配置全断时方向
    用 B23 冻结谓词的结构身份 (SHORT) 兜底 ⇒ 非臂方向仍放行 (P10 不连带)。读取走
    10s TTL 缓存构造 (不用 R-4 盲单例: unified 键热载不穿透单例, 会丢 ~60s 热载回滚)。

调用约定 (每个 auto_execute 轮次一次 cycle_state, 每条指令一次 check_size/check_entry):
    st = cycle_state(today_et_str, target_env)
    ok, reason = check_entry(st, direction=inst['direction'], entry_minute=_now_min)
    size = cap_size(st, size, entry_price, direction=inst['direction'])
    # 臂方向成交成功后: st["today_count"] += 1  (轮内唯一增量, P13)

方向口径 (2026-11-16 对双库实读): `playbook_instructions.direction ∈ {BUY, SELL_SHORT, SKIP}`
⇒ 与 `s1_arm_allowed_direction='SELL_SHORT'` 同词汇, 匹配成立; `trade_records.direction ∈
{LONG, SHORT}` 是另一套词汇, 只用于 `S1_WHERE` 人群谓词。两者不可互换。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _ledger(target_env: str) -> Path:
    """按臂别解析账本 (SSOT: `app/core/paths.py::get_trade_records_db_for_arm`)。"""
    from app.core.paths import get_trade_records_db_for_arm

    return Path(get_trade_records_db_for_arm(target_env))


class S1ConfigError(Exception):
    """授权配置键缺失/不可读 (C11 单源 fail-close)。

    授权限额唯一真源 = config/unified_config.yaml execution_parameters.s1_arm_*。
    原 DEFAULTS 同值双写 + 读失败静默回落 ⇒ 配置漂移/丢失时限额悄悄变值且零告警。
    现缺键即抛, 由各消费点转 fail-close (cycle_state degraded / check_entry 拒 /
    cap_size 0 / kill_reason 停), 绝不回落第二份数值。
    """


# C12: TTL 缓存构造, 不用 config.py 的 R-4 盲单例 —— 它只在 horizon 文件
# mtime 变更时重建, unified 键 (s1_arm_enabled 等) 变更不穿透 ⇒ 会丢 ~60s 热载
# 回滚能力。TTL=10s: 每指令 6+ 次构造降为 ~1 次/10s, 热载延迟仍远小于 60s 预算。
_CFG_TTL_SEC = 10.0
_cfg_cache: Tuple[float, Any] = (0.0, None)


def _cfg(key: str):
    """读 playbook.execution_parameters 拍平键; 读不到返回 None (cfg 转 S1ConfigError)。"""
    global _cfg_cache
    try:
        import time

        now = time.monotonic()
        ts, pc = _cfg_cache
        if pc is None or now - ts >= _CFG_TTL_SEC:
            from app.playbook.config import PlaybookConfig

            pc = PlaybookConfig()
            _cfg_cache = (now, pc)
        return pc.get(key)
    except Exception:
        return None


def cfg(key: str):
    v = _cfg(key)
    if v is None:
        raise S1ConfigError("s1_arm 授权配置键缺失/不可读: %s (单源=YAML, 无回落)" % key)
    return v

# 判据 B23 的 S1 人群 (与 scripts/s1_short_after_open_cohort.py 同一谓词; 改动须同步两处)
S1_WHERE = (
    "exit_time IS NOT NULL AND pnl_net IS NOT NULL "
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL') "
    "AND direction = 'SHORT'"
)


def enabled() -> bool:
    v = cfg("s1_arm_enabled")
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _connect(target_env: str):
    conn = sqlite3.connect("file:%s?mode=ro" % _ledger(target_env), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _et_minute(ts: str) -> Optional[int]:
    """entry_time → ET 当日分钟数 (库内混存多偏移, 禁写死 −4h)。"""
    if not ts:
        return None
    try:
        from app.utils.timezone import parse_iso_safe, to_et

        dt = parse_iso_safe(ts)
        if dt is None:
            return None
        et = to_et(dt)
        return et.hour * 60 + et.minute
    except Exception:
        return None


def open_exposure_usd(target_env: str, start_date: Optional[str] = None) -> float:
    """本实验的在仓名义额 (trade_records 未平仓行; 平仓即释放)。

    只计 `trade_date >= start_date` 的仓: 臂启动前遗留的旧仓 (SIM 期) 不计入帽,
    否则历史在仓会把新实验永久卡在敞口帽上 (实测 11-15 遗留在仓 $33.8k)。

    **不吞异常 (P9)**: 原实现 `except → return 0.0` 会让 $8,400 敞口帽在一次 SQLite
    锁忙后静默消失且无告警。读不出来 = 判不出来, 交由 `cycle_state` 转 degraded。
    谓词**不加方向过滤**: 与用户授权的"总敞口 ≤$8,400"同口径 (窗口内全部在仓);
    不过滤只会让帽更早绑定 = 更保守, 放宽属改授权上限, 须另行裁决。
    """
    if start_date is None:
        start_date = str(cfg("s1_arm_start_date"))
    conn = _connect(target_env)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(ABS(quantity * entry_price)), 0) FROM trade_records"
            " WHERE exit_time IS NULL AND trade_date >= ?",
            (start_date,),
        ).fetchone()
        return float(row[0] or 0.0)
    finally:
        conn.close()


def cohort_state(target_env: str, start_date: str, end_date: Optional[str] = None) -> Dict[str, Any]:
    """前瞻 cohort 的已平仓读数 (kill switch 与判据共用口径)。

    **不吞异常 (P9)**: 原实现 `except → {n:0, net:0.0}` 会让 `kill_reason` 判
    `0.0 <= -300` 假、`0 >= 15` 假 ⇒ 用户授权的 kill switch 恒不触发且无告警。
    `end_date` 可选尾界 (trade_date < end_date): 仅供历史参照测试钉住落档窗口,
    生产调用不传 ⇒ 语义逐字不变。
    """
    return _cohort_state_impl(target_env, start_date, arm_only=False, end_date=end_date)


# 臂属来源过滤 (DEB-20261116 C6): kill switch 人群只收本臂成交。live 库 SHORT 实测
# 全为 MANUAL (MU 手动仓), 且 PLAYBOOK_LIVE 行无 dual_arm 落库标记 (C13 未落) ⇒
# 以 source_type 为臂属谓词。sim 侧 PLAYBOOK_SIM 同样混有非臂流程, 同口径过滤。
_ARM_SOURCE = {"sim": "PLAYBOOK_SIM", "real": "PLAYBOOK_LIVE", "live": "PLAYBOOK_LIVE"}


def arm_cohort_state(target_env: str, start_date: str, end_date: Optional[str] = None) -> Dict[str, Any]:
    """臂属过滤的 cohort 读数 (C6): S1_WHERE ∧ source_type=本臂来源。

    不改 `S1_WHERE` 本身 (B23 人群谓词是冻结面, 与评估器脚本共用);
    本函数供真钱侧 kill switch 影子与签署日后的执行接线使用。
    """
    return _cohort_state_impl(target_env, start_date, arm_only=True, end_date=end_date)


def _cohort_state_impl(target_env: str, start_date: str, arm_only: bool,
                       end_date: Optional[str] = None) -> Dict[str, Any]:
    out = {"n": 0, "net": 0.0, "net_bps": None, "fee_bps": None}
    conn = _connect(target_env)
    try:
        sql = ("SELECT entry_time, exit_time, quantity, entry_price, realized_pnl,"
               " fees_est, pnl_net FROM trade_records"
               " WHERE " + S1_WHERE + " AND trade_date >= ?")
        args = [start_date]
        if end_date is not None:
            sql += " AND trade_date < ?"
            args.append(end_date)
        if arm_only:
            sql += " AND source_type = ?"
            args.append(_ARM_SOURCE[str(target_env).strip().lower()])
        rows = [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
    keep = [r for r in rows if (_et_minute(r["entry_time"]) or 0) >= int(cfg("s1_arm_min_entry_minute_et"))]
    if not keep:
        return out
    no = sum((r["quantity"] or 0) * (r["entry_price"] or 0) for r in keep)
    net = sum(r["pnl_net"] for r in keep)
    fee = sum(r["fees_est"] or 0 for r in keep)
    out.update(
        n=len(keep),
        net=round(net, 2),
        net_bps=round(net / no * 1e4, 2) if no else None,
        fee_bps=round(fee / no * 1e4, 2) if no else None,
    )
    return out


# 真钱侧停止条件 record-only 影子 (DEB-20261116 C5/C6, RFC-212 §5.6)。
# 默认路径仅供直接调用/测试; flow 接线一律显式传 module_file 派生路径
# (同 sizing_delta 范式) —— hermetic 沙箱改写模块 __file__ 后影子自动落 tmp,
# 不触写守卫 (tests/playbook 隔离契约)。
_REAL_KILL_SHADOW_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "s1_real_kill_shadow.jsonl"


def record_real_kill_shadow(today: str, shadow_path: Optional[Path] = None) -> None:
    """每轮读真钱账本一次, 落一行观测; **不拦截、不 halt、不写交易库**。

    执行接线 (kill_reason → halt_live_new_positions 持久化 + 镜像闸) 属签署日批,
    须先经本影子观测 + 用户裁 (DEB-20261116 §10 第 1 步)。独立于 s1_arm_enabled:
    关闸期也观测真钱账本的停止条件可读性。任何异常只留痕, 绝不影响交易。
    """
    from app.utils.timezone import now_et

    out_path = Path(shadow_path) if shadow_path is not None else _REAL_KILL_SHADOW_PATH
    row: Dict[str, Any] = {
        "ts": now_et().isoformat(timespec="seconds"), "today": today,
        "ledger": "ftseek_us_live.db", "mode": "record_only",
        "degraded": False, "error": None,
        "exposure_usd": None, "cohort": None, "kill_reason": None,
    }
    try:
        start = str(cfg("s1_arm_start_date"))
        row["exposure_usd"] = round(open_exposure_usd("real", start), 2)
        coh = arm_cohort_state("real", start)
        row["cohort"] = coh
        row["kill_reason"] = kill_reason(coh)
    except Exception as exc:
        row["degraded"] = True
        row["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("[S1臂] 真钱停止条件影子读库失败 (record-only, 不影响交易): %s",
                       exc, exc_info=True)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[S1臂] 真钱停止条件影子落盘失败 (不影响交易): %s", exc, exc_info=True)


def kill_reason(state: Dict[str, Any]) -> Optional[str]:
    """S1 方向性 / S2 经济性 两条停止条件 (B23 原文)。

    ⚠️ **S3 (实盘逐笔真实滑点中位 >15bps) 仍未接线 (P11 两段式)**:
    B23 要求 `exit_slippage_bps` + 入场追价 bps 两段合成。实读 (2026-11-16):
    `exit_slippage_bps` 在 `trade_records.metadata_json` 有 727/871 行; 入场追价 bps
    **原全库无持久化字段** ⇒ 2026-11-16 采集已落库 (P11 第一段:
    `playbook_service.compute_entry_chase_bps` → metadata `entry_chase_bps`,
    锚=盘前计划 reprice_from 优先, 正=不利, bps 10000 制, record-only)。
    **S3 接线 (第二段) 仍不做**: 须前瞻窗攒到样本 + 中位数口径裁决后接入;
    只用退出侧算会**低估**滑点 ⇒ 让 S3 更难触发 = 弱化 kill switch, 宁缺勿半。
    """
    n = state.get("n") or 0
    try:
        if state.get("net", 0.0) <= float(cfg("s1_arm_kill_cum_pnl_usd")):
            return "S2_ECON(cum_net=%.2f)" % state.get("net", 0.0)
        if n >= int(cfg("s1_arm_kill_after_n")) and (state.get("net_bps") or 0) < 0:
            return "S1_DIR(n=%d,net_bps=%.2f)" % (n, state.get("net_bps") or 0.0)
        return None
    except S1ConfigError:
        return "CONFIG_UNREADABLE(fail-close)"   # C11: 停止条件判不出 ⇒ 按停处理


def _today_arm_entries(target_env: str, today: str) -> int:
    """今日已成交的本臂笔数 (P13 重设计, 用户 2026-11-16 裁「一条账本一个锚点一个增量」)。

    谓词即语义: direction=SHORT ∧ source_type=本臂来源 ∧ trade_date=今日。
    方向/归属在谓词里天然正确 (DEB-20261116 C7 日笔数半边), 数真成交不数尝试,
    跨重启不丢。簿记时滞由调用点轮内唯一增量兜底 (见 auto_execute_flow 成交后)。
    """
    conn = _connect(target_env)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM trade_records"
            " WHERE trade_date = ? AND direction = 'SHORT' AND source_type = ?",
            (today, _ARM_SOURCE[str(target_env).strip().lower()]),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def cycle_state(today: str, target_env: str) -> Dict[str, Any]:
    """每轮评估一次; enabled=False 时所有字段留空且不查库。

    `target_env` **无默认值** (P8): 漏传即 TypeError, 不静默回落 sim 账本。
    日笔数自臂账本锚定 (P13): 不再接收外部全流向计数器 —— 原 today_opened_count
    形参已删, 多头成交不再吃空头臂额度。
    """
    st: Dict[str, Any] = {
        "enabled": False, "today": today, "today_count": 0,
        "exposure": 0.0, "killed": None, "cohort": {"n": 0, "net": 0.0},
        "target_env": str(target_env or ""), "degraded": False,
    }
    try:
        is_on = enabled()
    except S1ConfigError:
        # C11 fail-close: 连 enabled 都判不出 ⇒ 视为"开闸但降级", 拒本臂新仓,
        # 非臂方向仍由 check_entry 的方向门放行 (P10 不受影响)。方向载结构身份
        # (B23 冻结谓词 SHORT), 使下游方向判定不依赖已断的配置。
        st["enabled"] = True
        st["degraded"] = True
        st["allowed_direction"] = "SELL_SHORT"
        logger.critical("[S1臂] s1_arm_enabled 不可读 (单源=YAML, 无回落) ⇒ 按降级拒新仓",
                        exc_info=True)
        return st
    if not is_on:
        return st
    st["enabled"] = is_on
    try:
        st["allowed_direction"] = str(cfg("s1_arm_allowed_direction")).upper()
        st["single_cap_usd"] = float(cfg("s1_arm_single_notional_cap_usd"))
        st["exposure"] = open_exposure_usd(target_env)
        st["cohort"] = cohort_state(target_env, str(cfg("s1_arm_start_date")))
        st["today_count"] = _today_arm_entries(target_env, today)
    except S1ConfigError:
        # C11 fail-close: 授权键缺失/不可读 ⇒ 判不出限额 ⇒ 按降级拒本臂新仓。
        st["degraded"] = True
        logger.critical("[S1臂] 授权配置不可读 (单源=YAML, 无回落) ⇒ 本轮拒绝新开仓",
                        exc_info=True)
        return st
    except Exception:
        # P9 fail-close: 账本读不出来 ⇒ 敞口帽/kill switch/日笔数都无法判定。
        # 原实现两个查询各自吞异常返 0 ⇒ 两项限额静默失效且零告警 (RFC-212 §5 P9)。
        st["degraded"] = True
        logger.critical(
            "[S1臂] target_env=%s 账本读取失败, 敞口/kill switch/日笔数不可判定 "
            "⇒ 本轮拒绝新开仓 (fail-close)", target_env, exc_info=True)
        return st
    st["killed"] = kill_reason(st["cohort"])
    return st


def check_entry(st: Dict[str, Any], direction: str, entry_minute: Optional[int]) -> Tuple[bool, str]:
    """臂方向的五道门: 时段/日笔数/敞口/kill switch/降级。返回 (放行?, 拒绝原因)。

    非臂方向**一律放行且不施加任何 S1 约束** (P10, 用户 2026-11-16 裁选项 1)。
    方向判定必须在 killed/degraded 之前: 否则本臂的停止条件会连带停掉无关方向的流量。
    """
    if not st.get("enabled"):
        return True, ""
    try:
        allowed = str(st.get("allowed_direction") or cfg("s1_arm_allowed_direction")).upper()
    except S1ConfigError:
        # C11 × P10: 配置全断时方向用**结构身份**兜底 —— 本臂=SHORT 是 B23 冻结谓词
        # (S1_WHERE direction='SHORT') 写死的实验身份, 不是第二份配置数值; 阈值仍
        # fail-close。如此非臂方向照常放行, 不重演"开闸即只做空"的 P10 病灶。
        allowed = "SELL_SHORT"
    if str(direction or "").upper() != allowed:
        return True, ""
    try:
        if st.get("degraded"):
            return False, "s1_arm_degraded(账本不可读, fail-close)"
        if st.get("killed"):
            return False, "s1_arm_killed:%s" % st["killed"]
        if entry_minute is None or int(entry_minute) < int(cfg("s1_arm_min_entry_minute_et")):
            return False, "s1_arm_before_open(%s)" % entry_minute
        cap_n = int(cfg("s1_arm_max_entries_per_day"))
        if st.get("today_count", 0) >= cap_n:
            return False, "s1_arm_daily_count(%d>=%d)" % (st.get("today_count", 0), cap_n)
        cap_x = float(cfg("s1_arm_max_open_exposure_usd"))
        if (st.get("exposure") or 0.0) >= cap_x:
            return False, "s1_arm_exposure(%.0f>=%.0f)" % (st.get("exposure") or 0.0, cap_x)
        return True, ""
    except S1ConfigError:
        return False, "s1_arm_config_missing(授权键不可读, fail-close)"


def cap_size(st: Dict[str, Any], size: int, entry_price: float, direction: str) -> int:
    """按单笔名义帽缩股 (整股向下取整); 不放大。enabled=False 原样返回。

    `direction` **无默认值** (P10): 漏传即 TypeError。否则非臂方向会被 `check_entry`
    放行后仍在调用点被本函数按 $4,200 缩股 ⇒ 多头仓位被空头臂的帽砍掉。
    """
    if not st.get("enabled") or not size:
        return size
    try:
        allowed = str(st.get("allowed_direction") or cfg("s1_arm_allowed_direction")).upper()
    except S1ConfigError:
        allowed = "SELL_SHORT"               # 结构身份兜底 (同 check_entry, B23 冻结谓词)
    try:
        if str(direction or "").upper() != allowed:
            return size                      # P10: 非臂方向不施加单笔帽
        if entry_price <= 0:
            # P9 族 fail-closed (DEB-20261116 C8): 无价 ⇒ 名义额不可算 ⇒ 一股不放行。
            # 原实现与 enabled/size 混在同一 guard 里 return size = fail-open, 与本模块
            # degraded 返 0 的纪律相反。调用点以 s1_arm_no_price 落痕。
            return 0
        if st.get("degraded"):
            return 0                         # P9: 敞口余量不可判定 ⇒ 一股都不放行
        cap_usd = float(st.get("single_cap_usd") or cfg("s1_arm_single_notional_cap_usd"))
        room = min(cap_usd, max(0.0, float(cfg("s1_arm_max_open_exposure_usd")) - (st.get("exposure") or 0.0)))
        max_qty = int(math.floor(room / entry_price))
        return int(min(size, max_qty))
    except S1ConfigError:
        return 0                             # C11: 授权键不可读 ⇒ 一股不放行