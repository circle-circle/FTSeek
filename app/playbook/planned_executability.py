"""生成期「估算股数 / 费用覆盖 / 可执行性」预览 — P3 第二批 P3.3/P3.4/P3.5 (record-only).

背景 (DEB-20260910 §18/§19/§20): P3 第一批把 UE 的**生成值** (entry/SL/TP) 留了档, 计划
P3 要求的后三项 —— 估算股数、费用覆盖、可执行性 —— 当时刻意没做, 因为三个前置未定:
  ① 估算股数要读资本状态, 而计划明禁「逐候选调用会读取资本状态的完整
     compute_instruction_values()」;
  ② 借券费有两套日基准 (fee_model 365 自然日 / CostModel 252 交易日), 计划要求显式转换;
  ③「`fees_source='est'` 是否覆盖借券」未查。
本节把三条前置钉死后落地 (取证数字见 DEB §20), 产物**全部是审计字段**, 不进任何门、
排序、定价或仲裁判据; 确定性无效项的**拒绝闸**是独立一批 (计划 P3 第三批), 此处不拒。

三条口径决定
------------
· **权益每批读一次**: 只读 URI 取 `capital_state` 最新行, **不实例化**
  `CapitalSafetySystem` —— 后者 `__init__` 会 `_ensure_db_table()` 开写连接
  (`capital_safety.py:139`), cron fork 的 UE 进程不该往生产库引入新的写连接。
  代价: 拿不到 broker 实时刷新, 只有 DB 里最后一次落盘的净值 ⇒ 故记 `equity_asof`
  与 `equity_age_hours`, 陈旧即显式标 `stale`, 不拿旧净值冒充当前净值。
  ⚠ `capital_state.last_update` 由裸 now（`datetime.now`，见 `capital_safety.py:180`）写,
  是**服务器本地朴素时间 (CST)**, 不是 ET。故年龄不拿 `now_et()` 直接相减 (会凭空差 12h
  ⇒ 每条快照都被误判陈旧), 而是把两边都化成 epoch: naive 串的 `timestamp()` 按本地时区
  解释 = 写入方同一把尺, 与 `time.time()` 同尺可比。本模块因此**不出现裸 now**
  (`tests/test_naive_datetime_now_ratchet.py` 的 BASELINE_NOW 只降不升)。

· **股数复用 sizing SSOT**: 调纯函数 `compute_position_size()`
  (`instruction_generator.py:37`), 乘数按执行侧同一张表推 (`pipeline.py:4135-4197`),
  不复刻 sizing 数学。实测 (DEB §20.3): 近 14 条已触发 UE 行中 11 条与
  `int(max_position_pct × equity / entry)` 逐位吻合, 其余 3 条差 1 股 (高价股 int 截断
  + 逐日净值漂移) ⇒ **UE 股数由名义上限绑定, `risk_pct=0.5%` 预算项在当前 SL 宽度
  (BUY 4.5% / SHORT 5.5%) 下不生效** (预算名义 = equity×0.005/0.045 = 11.1% > 6% 帽)。
  ⇒ 必须记 `size_basis`, 否则读账的人会以为风险预算在起作用。

· **费用只走 fee_model**: `app/execution/fee_model` 是生产 SSOT (实盘 order_fee_query
  逐位验证过)。**不走 `CostModel`** —— 它的借券项是 `fraction 年率 × 持有交易日/252`
  (`cost_model.py:177`), 与 fee_model 的 `percent 年率 × int(自然日)/365`
  (`fee_model.py:130-140`) 三重不一致 (单位/基准/取整), 且它活在**退出路径**
  (`position_exit_monitor` → `trailing_stop.py:151`), 计划明禁改退出参数。
  ⇒ 两套数**都记** (`borrow.usd` 与 `borrow.usd_if_cost_model_basis`), 谁也不改谁。

诚实边界: `estimate_round_trip` **不含借券** (借券是独立函数 `accrue_borrow_fee`, 生产
账路径 `pipeline.py:6596` 写 `fees_est` 时不调它) ⇒ 本模块把借券单列, 并在缺项时置
`cost_complete=False`; **不计算净期望** (计划: 缺成本数据不产生伪精确净期望)。
点差/冲击无 bid-ask 与 ADV 数据时只标 unknown, 不按零点差计算 (计划 P1 §3)。

作者: FTSeek Quant Team  创建: 2026-09-13
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 预览 blob 的口径版本 (与 UE 的 PLANNED_PREVIEW_VERSION 同步升版: blob 从此含
# size/cost/exec 三段)。basis 版本单列 —— 日后只改借券基准或上限归因时, 升 basis 不升 blob。
PLANNED_EXEC_BASIS_VERSION = "planned_exec_v1"

# 借券日基准: fee_model SSOT = 365 自然日 (富途日息惯例), CostModel = 252 交易日。
BORROW_DAY_BASIS_FEE_MODEL = 365
BORROW_DAY_BASIS_COST_MODEL = 252
BORROW_BASIS_RATIO = BORROW_DAY_BASIS_FEE_MODEL / BORROW_DAY_BASIS_COST_MODEL  # 1.4484

# 水位乘数缺省 — 与 capital_safety.WaterLevelConfig 的 dataclass 缺省逐值一致
# (parity 由 tests/test_ue_planned_executability.py::TestWaterMultiplierParity 钉住,
#  对 5 个水位逐个比对 CapitalSafetySystem.get_position/risk_multiplier)。
_WATER_DEFAULTS: Dict[str, Dict[str, float]] = {
    "green": {"pos_mult": 1.0, "risk_mult": 1.0},
    "yellow": {"pos_mult": 0.5, "risk_mult": 0.5},
    "orange": {"pos_mult": 0.25, "risk_mult": 0.25},
    "red": {"pos_mult": 0.0, "risk_mult": 0.0},
    "blackout": {"pos_mult": 0.0, "risk_mult": 0.0},
}

# regime 乘数表 — 与 pipeline.py:4147/4155 同一张表 (CAUTIOUS 走配置 cautious_mult)。
_REGIME_MULTS = {"RISK_ON": 1.0, "RISK_OFF": 0.3, "NO_TRADE": 0.0}
_REGIME_DEFAULT_MULT = 0.5

# 权益快照陈旧阈值 (小时): 超过即标 stale。净值只在主进程 accinfo_query 刷新时落盘,
# 周末/停机期间必然陈旧 —— 陈旧不等于错, 但必须让读账的人看见。
EQUITY_STALE_HOURS = 24.0

_SHORT_DIRECTIONS = ("SHORT", "SELL_SHORT", "SELL")


def _positive_finite(value: Any) -> Optional[float]:
    """正有限浮点, 否则 None。bool 不是数 (True 会被 float() 吃成 1.0)。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return round(float(value), digits) if value is not None else None


def _now_et() -> datetime:
    """当前时刻 (ET aware) — 本仓主口径。容器不可用时退到 aware UTC, **不退到裸 now**
    (naive CST 与 ET 口径混比会差 12h, 且抬高 BASELINE_NOW 棘轮)。
    """
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception as exc:
        logger.warning("now_et unavailable, falling back to aware UTC: %s", exc, exc_info=True)
        return datetime.now(timezone.utc)


def _as_dict(value: Any) -> Dict[str, Any]:
    """非 dict 一律当空 dict — 上游 (screening/preview) 传错类型时降级, 不抛。

    预览是 record-only 附加物: 它抛异常会连带把注入搞挂, 这比"少一条预览"严重得多。
    """
    return value if isinstance(value, dict) else {}


# ─────────────────────────── 每批一次的上下文读取 ───────────────────────────
def water_multiplier(water_level: Optional[str], cfg: Optional[Dict[str, Any]] = None) -> float:
    """水位乘数 = min(pos_mult, risk_mult), 与 capital_safety 的取值语义一致。

    执行侧写的是 `min(cs.get_position_multiplier(), cs.get_risk_multiplier())`
    (`pipeline.py:4141`); 两个映射都来自 `risk.capital_safety.water_levels.<level>`
    (`capital_safety.py:655-692`), 此处读同一 yaml 路径 + 同一 dataclass 缺省。
    未知水位 ⇒ 返回 0.0 (与 SSOT 的 `multipliers.get(level, 0.0)` 同形: 不认识就不给仓位)。
    """
    level = str(water_level or "").strip().lower()
    if not level:
        return 0.0
    defaults = _WATER_DEFAULTS.get(level)
    if defaults is None:
        return 0.0
    pos_mult, risk_mult = defaults["pos_mult"], defaults["risk_mult"]
    levels = (cfg or {}).get("water_levels") or {}
    node = levels.get(level)
    if isinstance(node, dict):
        pos_mult = float(node.get("pos_mult", pos_mult))
        risk_mult = float(node.get("risk_mult", risk_mult))
    return min(pos_mult, risk_mult)


def regime_multiplier(regime_name: Optional[str], ctx: Optional[Dict[str, Any]] = None) -> float:
    """regime 乘数 — 与 pipeline.py:4147/4155 同一张表。

    键空间是大写枚举名 (`_normalize_regime_str` 取 `.name`, `pipeline.py:1252-1265`);
    调用方传 `MarketRegime.regime.value` (小写) 也可以, 此处统一 upper()。
    CAUTIOUS 走配置 `cautious_mult` (2026-08-06 用户指示 sim 采集期不降仓, 现值 1.0)。
    """
    name = str(regime_name or "").strip().upper()
    if not name:
        return _REGIME_DEFAULT_MULT
    if name == "CAUTIOUS":
        return float((ctx or {}).get("cautious_mult", 1.0))
    return float(_REGIME_MULTS.get(name, _REGIME_DEFAULT_MULT))


def read_sizing_context(db_path: Optional[str] = None, *,
                        now: Optional[datetime] = None,
                        expected_trade_env: Optional[int] = None) -> Dict[str, Any]:
    """每批**读一次**的 sizing 上下文 (只读, 不实例化 CapitalSafetySystem)。

    返回面自带 provenance: equity 从哪来 (`capital_state_db` / `config_risk_base_capital`)、
    何时落的盘 (`equity_asof`, 服务器本地朴素时间)、多陈旧 (`equity_age_hours` / `stale`)、
    水位是什么 (`water_level` → `water_mult`)。任何一步失败都不抛, 退化成 `state='degraded'`
    并把原因写进 `notes` —— 预览缺数不应该让注入失败。
    """
    ctx: Dict[str, Any] = {
        "state": "degraded",
        "equity": None,
        "equity_origin": None,
        "equity_asof": None,
        "equity_age_hours": None,
        "equity_stale": None,
        "water_level": None,
        "water_mult": 0.0,
        "trade_env": None,
        "expected_trade_env": expected_trade_env,
        "risk_pct": None,
        "max_position_pct": None,
        "risk_base_capital": None,
        "cautious_mult": 1.0,
        "confidence_sizing_enabled": False,
        "model_disagreement_downsize_factor": 1.0,
        "notes": [],
        "read_at": (now or _now_et()).isoformat(timespec="seconds"),
        "basis_version": PLANNED_EXEC_BASIS_VERSION,
    }

    # ── 配置面 (与执行侧同一 accessor: get_playbook_config 扁平合并 + risk_parameters 点路径) ──
    try:
        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()
        ctx["risk_pct"] = float(pb_cfg.get("risk_pct", 0.5))
        ctx["max_position_pct"] = float(pb_cfg.get("max_position_pct", 0.06))
        ctx["risk_base_capital"] = float(pb_cfg.get("risk_base_capital", 82000.0))
        ctx["min_confidence"] = float(pb_cfg.get("min_confidence", 60) or 60)
        ctx["confidence_sizing_enabled"] = bool(pb_cfg.get("confidence_sizing_enabled", False))
    except Exception as exc:
        ctx["notes"].append(f"playbook_config_unavailable:{type(exc).__name__}")
        logger.warning("sizing context: playbook config unavailable: %s", exc, exc_info=True)
    try:
        from app.core.config_container import get_config
        params = get_config().get_dotted("playbook.risk_parameters", {}) or {}
        ctx["cautious_mult"] = float(params.get("cautious_mult", 1.0) or 1.0)
        ctx["model_disagreement_downsize_factor"] = float(
            params.get("model_disagreement_downsize_factor", 1.0) or 1.0)
        ctx["capital_safety_cfg"] = get_config().get_dotted("risk.capital_safety", {}) or {}
    except Exception as exc:
        ctx["notes"].append(f"unified_config_unavailable:{type(exc).__name__}")
        logger.warning("sizing context: unified config unavailable: %s", exc, exc_info=True)

    # ── 期望账户环境 (与 capital_safety 同源: AUTO_TRADE_ACCOUNT, sim ⇒ trade_env=1) ──
    if expected_trade_env is None:
        import os
        ctx["expected_trade_env"] = 1 if os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower() == "sim" else 0

    # ── 权益面 (只读 URI; 不用 CapitalSafetySystem, 见模块 docstring) ──
    row: Optional[sqlite3.Row] = None
    try:
        from app.core.db_utils import get_main_db
        path = db_path or str(get_main_db())
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT current_equity, water_level, trade_env, last_update "
                "FROM capital_state ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        ctx["notes"].append(f"capital_state_unreadable:{type(exc).__name__}")
        logger.warning("sizing context: capital_state read failed: %s", exc, exc_info=True)

    if row is None:
        ctx["notes"].append("capital_state_empty")
    else:
        ctx["trade_env"] = row["trade_env"]
        ctx["water_level"] = (row["water_level"] or "").strip().lower() or None
        ctx["equity_asof"] = row["last_update"] or None
        saved_env = row["trade_env"]
        if saved_env is not None and saved_env != ctx["expected_trade_env"]:
            # 与 SSOT 同形 (capital_safety.py:761-767): 环境切换过 ⇒ 旧净值不可用。
            ctx["notes"].append(f"trade_env_mismatch(saved={saved_env})")
        else:
            equity = _positive_finite(row["current_equity"])
            if equity is None:
                ctx["notes"].append("capital_state_equity_non_positive")
            else:
                ctx["equity"] = equity
                ctx["equity_origin"] = "capital_state_db"

    if ctx["equity"] is None:
        fallback = _positive_finite(ctx.get("risk_base_capital"))
        if fallback is not None:
            ctx["equity"] = fallback
            ctx["equity_origin"] = "config_risk_base_capital"
            ctx["notes"].append("equity_from_config_fallback")

    # 年龄按写入方同一时钟 (服务器本地朴素时间) 算 — 见 docstring 的 CST/ET 警告。
    if ctx["equity_asof"]:
        try:
            stamp = datetime.fromisoformat(str(ctx["equity_asof"]))
            # 两边都化 epoch: naive 串按本地时区 (CST) 解释 = 写入方同一把尺;
            # 注入的 now 若是 aware ET, timestamp() 也是精确 epoch ⇒ 混用不会错尺。
            base_epoch = now.timestamp() if now is not None else time.time()
            age_hours = (base_epoch - stamp.timestamp()) / 3600.0
            ctx["equity_age_hours"] = round(age_hours, 2)
            ctx["equity_stale"] = bool(age_hours > EQUITY_STALE_HOURS or age_hours < -1.0)
        except Exception as exc:
            ctx["notes"].append(f"equity_asof_unparsable:{type(exc).__name__}")
            logger.warning("sizing context: equity_asof unparsable: %s", exc, exc_info=True)

    ctx["water_mult"] = water_multiplier(ctx["water_level"], ctx.get("capital_safety_cfg"))
    ctx.pop("capital_safety_cfg", None)
    ctx["state"] = "ok" if (ctx["equity"] is not None
                            and ctx["equity_origin"] == "capital_state_db"
                            and not ctx["equity_stale"]) else "degraded"
    return ctx


def load_borrow_rates(path: Optional[str] = None, *, asof: Optional[datetime] = None,
                      max_age_days: float = 7.0) -> Dict[str, Dict[str, Any]]:
    """借券费率快照 (只读 jsonl) → {symbol: {rate_pct, rate_asof, shortable, pool, age_days}}.

    来源 `scripts/borrow_rate_snapshot.py` (cron 每日对**当日活跃池**取空头池费率)。
    ⚠ 覆盖面的诚实边界: 快照只覆盖活跃池, 而 UE 的价值恰在**榜外发现** ⇒ 实测 UE 指令行
    `candidate_screening.shortable` 覆盖率 0/183 (DEB §20.4) ⇒ 多数 UE 候选拿不到费率,
    此时按 unknown 记, **不套用 3% 中位数当已知** (计划: 借券未知不能当可借)。
    取每个 symbol 在 `asof` 之前最近的一条; 超过 `max_age_days` 标 `stale=True` 但仍返回
    (陈旧费率比编造费率诚实, 由调用方决定是否采信)。
    """
    try:
        from app.core.paths import get_data_dir
        default_path = Path(get_data_dir()) / "shadow" / "borrow_rate_series.jsonl"
    except Exception as exc:
        default_path = Path("data/shadow/borrow_rate_series.jsonl")
        logger.warning("borrow rates: data dir unresolved, using relative path: %s",
                       exc, exc_info=True)
    file_path = Path(path) if path else default_path
    out: Dict[str, Dict[str, Any]] = {}
    if not file_path.exists():
        return out
    cutoff = asof or _now_et()
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                symbol = str(rec.get("symbol") or "").strip().upper()
                day = str(rec.get("date") or "")[:10]
                if not symbol or not day:
                    continue
                prev = out.get(symbol)
                if prev and str(prev.get("rate_asof") or "") >= day:
                    continue
                age_days: Optional[float] = None
                try:
                    age_days = (cutoff.date() - datetime.fromisoformat(day).date()).days
                except ValueError:
                    age_days = None
                out[symbol] = {
                    "rate_pct": _positive_finite(rec.get("fee_rate")),
                    "rate_asof": day,
                    "shortable": rec.get("shortable") if isinstance(rec.get("shortable"), bool) else None,
                    "pool": _positive_finite(rec.get("pool")),
                    "age_days": age_days,
                    "stale": bool(age_days is None or age_days > max_age_days),
                    "origin": "borrow_rate_series_jsonl",
                }
    except OSError as exc:
        logger.warning("borrow rates: read failed for %s: %s", file_path, exc, exc_info=True)
        return {}
    return out


# ─────────────────────────── 逐候选纯函数 (零 IO) ───────────────────────────
def estimate_size(*, entry: Any, stop_loss: Any, direction: str, ctx: Dict[str, Any],
                  confidence: float = 0.0) -> Dict[str, Any]:
    """估算股数 — 复用 sizing SSOT `compute_position_size()`, 并归因是哪一个约束绑定。

    `size_basis` 是本函数的主要产物: 实测 UE 股数由 `max_position_pct` 名义上限绑定,
    不是由 `risk_pct` 风险预算绑定 (见模块 docstring)。不记这个归因, 库里的
    `risk_amount` (派生列 = |entry−SL|×size, `pipeline.py:4253`) 会被误读成风险预算。
    """
    from app.playbook.instruction_generator import compute_position_size

    out: Dict[str, Any] = {
        "state": "not_computed", "basis_version": PLANNED_EXEC_BASIS_VERSION,
        "size": None, "size_basis": None, "truncated_by_cap": None,
        "notional": None, "cap_notional": None, "risk_budget_notional": None,
        "per_share_risk": None, "risk_amount_derived": None,
        "geometry_ok": None, "multipliers": None, "equity": ctx.get("equity"),
        "equity_origin": ctx.get("equity_origin"), "equity_stale": ctx.get("equity_stale"),
        "risk_pct": ctx.get("risk_pct"), "max_position_pct": ctx.get("max_position_pct"),
        "is_real_position": False,
    }
    entry_value = _positive_finite(entry)
    sl_value = _positive_finite(stop_loss)
    if entry_value is None or sl_value is None:
        out["state"] = "not_computed_invalid_price"
        return out
    equity = _positive_finite(ctx.get("equity"))
    if equity is None:
        out["state"] = "not_computed_no_equity"
        return out
    per_share_risk = abs(entry_value - sl_value)
    if per_share_risk <= 0:
        out["state"] = "not_computed_zero_risk_distance"
        return out

    is_short = str(direction or "").upper() in _SHORT_DIRECTIONS
    # 几何方向核对 (record-only): BUY 的 SL 应在入场下方, SHORT 应在上方。
    # 此处**只记不拒** —— 拒绝闸是 P3 第三批 (计划要求影子先行, 不与审计字段同批)。
    out["geometry_ok"] = bool(sl_value < entry_value) if not is_short else bool(sl_value > entry_value)

    regime_mult = regime_multiplier(ctx.get("regime"), ctx)
    water_mult = float(ctx.get("water_mult", 0.0) or 0.0)
    risk_pct = float(ctx.get("risk_pct") or 0.0)
    max_position_pct = float(ctx.get("max_position_pct") or 0.0)
    conf_factor = 1.0
    if bool(ctx.get("confidence_sizing_enabled", False)) and confidence > 0:
        conf_base = float(ctx.get("min_confidence", 60) or 60)
        if conf_base > 0:
            conf_factor = max(0.6, min(1.4, confidence / conf_base))
    if not is_short and confidence < 50:
        # 执行侧同形 (pipeline.py:4181-4195): BUY 且 conf<50 = "2模型方向分歧" 代理,
        # 乘 model_disagreement_downsize_factor (现值 1.0 = 不降, 2026-08-06 用户指示)。
        conf_factor *= float(ctx.get("model_disagreement_downsize_factor", 1.0) or 1.0)

    size = compute_position_size(
        equity, risk_pct, entry_value, sl_value, max_position_pct,
        regime_mult=regime_mult, water_mult=water_mult, conf_factor=conf_factor,
    )
    risk_amount = equity * (risk_pct / 100.0) * regime_mult * water_mult * conf_factor
    risk_size = int(risk_amount / per_share_risk)
    cap_notional = equity * max_position_pct if max_position_pct > 0 else None
    truncated = bool(cap_notional is not None and risk_size * entry_value > cap_notional)

    out.update({
        "state": "computed",
        "size": int(size),
        "size_basis": "max_position_pct_cap" if truncated else "risk_budget",
        "truncated_by_cap": truncated,
        "notional": _round(int(size) * entry_value, 2),
        "cap_notional": _round(cap_notional, 2),
        "risk_budget_notional": _round(risk_size * entry_value, 2),
        "risk_size_uncapped": int(risk_size),
        "per_share_risk": _round(per_share_risk, 4),
        # 与 DB 的 risk_amount 列同定义 (派生量, 不是风险预算) ⇒ 可直接对账。
        "risk_amount_derived": _round(int(size) * per_share_risk, 2),
        "multipliers": {
            "regime_mult": _round(regime_mult, 4),
            "regime": ctx.get("regime"),
            "water_mult": _round(water_mult, 4),
            "water_level": ctx.get("water_level"),
            "conf_factor": _round(conf_factor, 4),
        },
    })
    return out


def estimate_borrow(*, size: Any, entry: Any, direction: str, symbol: str = "",
                    horizon_hours: Any = None,
                    borrow_rates: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """借券费 — fee_model 365 自然日基准, 并给出 CostModel 252 基准的等价值 (显式转换)。

    `hold_days` 用模型自己声明的 `horizon_hours` (UE 指令 157/183 有值, 且以 4–8h 为主
    ⇒ `int(hold_days)=0` ⇒ 计划内借券为 $0)。这不是"没有借券成本": 实测 278 条已平仓
    SHORT 中 41 条**实际隔夜** (ET 本地日口径), 计划内日内的假设与实现不符 ⇒
    故 `overnight_risk=True` 显式留痕, 由退出侧的真实持有天数另算 (DEB §20.4)。
    """
    from app.execution.fee_model import accrue_borrow_fee

    out: Dict[str, Any] = {
        "state": "not_applicable_long", "usd": 0.0,
        "basis_days": BORROW_DAY_BASIS_FEE_MODEL,
        "rate_pct": None, "rate_asof": None, "rate_origin": None, "rate_stale": None,
        "shortable": None, "hold_days": None, "hold_days_origin": None,
        "usd_if_cost_model_basis": None, "cost_model_basis_days": BORROW_DAY_BASIS_COST_MODEL,
        "overnight_risk": False,
    }
    if str(direction or "").upper() not in _SHORT_DIRECTIONS:
        return out

    qty = _positive_finite(size)
    price = _positive_finite(entry)
    snapshot = _as_dict(borrow_rates).get(str(symbol or "").strip().upper()) or {}
    out["shortable"] = snapshot.get("shortable")
    out["rate_asof"] = snapshot.get("rate_asof")
    out["rate_origin"] = snapshot.get("origin")
    out["rate_stale"] = snapshot.get("stale")
    rate_pct = _positive_finite(snapshot.get("rate_pct"))
    out["rate_pct"] = rate_pct

    hours = _positive_finite(horizon_hours)
    if hours is None:
        out["state"] = "unknown_hold"
        out["overnight_risk"] = True
        return out
    hold_days = hours / 24.0
    out["hold_days"] = _round(hold_days, 4)
    out["hold_days_origin"] = "model_horizon_hours"
    out["overnight_risk"] = bool(hold_days >= 1.0)

    if rate_pct is None:
        out["state"] = "unknown_rate"
        return out
    if qty is None or price is None:
        out["state"] = "unknown_size_or_price"
        return out

    usd = accrue_borrow_fee(qty, price, rate_pct, hold_days)
    out["usd"] = _round(usd, 4)
    out["state"] = "known" if int(hold_days) > 0 else "zero_planned_intraday"
    # CostModel 口径等价值: fraction 年率 × **分数**持有交易日/252 (不 int 截断)。
    # 记它是为了能与退出路径 trailing_stop 的 cost_bps 对账, 不是为了替换它。
    out["usd_if_cost_model_basis"] = _round(
        qty * price * (rate_pct / 100.0) * hold_days / BORROW_DAY_BASIS_COST_MODEL, 4)
    if snapshot.get("stale"):
        out["state"] = "known_stale_rate"
    return out


def estimate_cost(*, entry: Any, take_profit: Any, size: Any, direction: str,
                  symbol: str = "", horizon_hours: Any = None,
                  borrow_rates: Optional[Dict[str, Dict[str, Any]]] = None,
                  screening: Optional[Dict[str, Any]] = None,
                  trade_date: Optional[str] = None) -> Dict[str, Any]:
    """费用覆盖 — 逐项记录佣金/交收/监管/借券与**未知项**, 不算净期望。

    `total_cost_usd` 只含**确定项** (fee_model 费率表 + 借券计提); 点差/冲击无数据时单列
    `unknown_items`, 并按零点差计算是计划明禁的 ⇒ `all_in_cost_usd=None`、
    `cost_complete=False`。`profit_fee_ratio` 不复算 —— `admission.evaluate()` 已产出同名
    指标 (口径=仅费率表往返费, 不含借券), 此处另给 `coverage_ratio` (含借券) 并写明差异。
    """
    from app.execution.fee_model import estimate_round_trip

    out: Dict[str, Any] = {
        "state": "not_computed", "basis_version": PLANNED_EXEC_BASIS_VERSION,
        "total_cost_usd": None, "all_in_cost_usd": None, "cost_complete": False,
        "items_usd": None, "unknown_items": [], "fee_bps": None,
        "gross_at_tp_usd": None, "coverage_ratio": None,
        "notional": None, "exit_price_assumed": None, "borrow": None,
        "spread_bps": None, "liquidity_usd_equiv": None,
        "source": "app.execution.fee_model",
    }
    price = _positive_finite(entry)
    qty = _positive_finite(size)
    if price is None or qty is None or qty < 1:
        out["state"] = "not_computed_no_size_or_price"
        return out

    is_short = str(direction or "").upper() in _SHORT_DIRECTIONS
    rt_direction = "SHORT" if is_short else "LONG"
    try:
        round_trip = estimate_round_trip(qty=float(qty), entry_price=price, exit_price=price,
                                         direction=rt_direction, trade_date=trade_date)
    except Exception as exc:
        out["state"] = "not_computed_fee_model_error"
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("cost coverage: fee_model failed for %s: %s", symbol, exc, exc_info=True)
        return out

    entry_items = round_trip.get("entry") or {}
    exit_items = round_trip.get("exit") or {}
    fee_usd = float(round_trip.get("total") or 0.0)
    items = {key: _round(float(entry_items.get(key, 0.0) or 0.0)
                         + float(exit_items.get(key, 0.0) or 0.0), 4)
             for key in ("commission", "settlement", "sec", "taf", "cat")}
    notional = qty * price

    borrow = estimate_borrow(size=qty, entry=price, direction=rt_direction, symbol=symbol,
                             horizon_hours=horizon_hours, borrow_rates=borrow_rates)
    borrow_usd = float(borrow.get("usd") or 0.0)
    unknown = ["spread", "impact"]
    spread_bps = None
    liquidity_usd = None
    screening = _as_dict(screening)
    bid = _positive_finite(screening.get("bid"))
    ask = _positive_finite(screening.get("ask"))
    if bid is not None and ask is not None and ask >= bid:
        mid = (bid + ask) / 2.0
        if mid > 0:
            # 往返点半差 ×2 = 一个完整来回的点差成本 (与 CostModel 的双边口径同形)。
            spread_bps = (ask - bid) / mid * 1e4
            liquidity_usd = notional * spread_bps / 1e4
            unknown.remove("spread")
    if borrow.get("state") in ("unknown_rate", "unknown_hold", "unknown_size_or_price",
                               "known_stale_rate"):
        unknown.append("borrow")
        if borrow.get("shortable") is None and is_short:
            unknown.append("borrow_availability")

    total_cost = fee_usd + borrow_usd
    gross_at_tp = None
    tp_value = _positive_finite(take_profit)
    if tp_value is not None:
        gross_at_tp = abs(tp_value - price) * qty
    out.update({
        "state": "computed",
        "items_usd": items,
        "borrow": borrow,
        "total_cost_usd": _round(total_cost, 4),
        "notional": _round(notional, 2),
        # 出场价决策时未知 ⇒ 双腿同价保守估 (与 admission.estimate_round_trip_fee 同口径)。
        "exit_price_assumed": _round(price, 4),
        "fee_bps": _round(fee_usd / notional * 1e4, 2) if notional > 0 else None,
        "cost_bps": _round(total_cost / notional * 1e4, 2) if notional > 0 else None,
        "gross_at_tp_usd": _round(gross_at_tp, 2),
        "coverage_ratio": (_round(gross_at_tp / total_cost, 2)
                           if gross_at_tp is not None and total_cost > 0 else None),
        "spread_bps": _round(spread_bps, 2),
        "liquidity_usd_equiv": _round(liquidity_usd, 4),
        "all_in_cost_usd": _round(total_cost + liquidity_usd, 4) if liquidity_usd is not None else None,
        "unknown_items": unknown,
        "cost_complete": not unknown,
        "double_count_guard": "borrow_included_once_via_fee_model_accrue;cost_model_not_used",
    })
    return out


def assess_executability(*, symbol: str, direction: str, entry: Any, stop_loss: Any,
                         take_profit: Any, size: Any, confidence: float = 0.0,
                         trade_date: Optional[str] = None,
                         screening: Optional[Dict[str, Any]] = None,
                         cost: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """可执行性 — 复用**纯函数** `admission.evaluate()`, 此处 `applied_here=False`。

    执行侧的真门在 `admission.apply()` (enforce=true 时把 REJECT 改写成 SKIP,
    `admission.py:199-233`); 本函数只在生成期把同一判据的读数留档, **不改方向、不改数值**,
    也不新增任何门 (计划 P3: 不启用新的 profit/fee 硬门)。附带记录 admission 看不见的
    未知项 (点差/借券可得性/P1 初筛状态), 让"能不能执行"与"缺什么数据"分开。
    """
    from app.playbook import admission

    out: Dict[str, Any] = {
        "state": "not_assessed", "verdict": None, "reasons": [], "metrics": {},
        "enforce_at_execution": None, "applied_here": False,
        "screening_status": _as_dict(screening).get("status"),
        "screening_reasons": _as_dict(screening).get("reasons") or [],
        "unknowns": [], "basis_version": PLANNED_EXEC_BASIS_VERSION,
    }
    inst = {
        "symbol": symbol, "direction": direction, "date": trade_date,
        "entry_price": _positive_finite(entry), "stop_loss": _positive_finite(stop_loss),
        "take_profit": _positive_finite(take_profit),
        "position_size": _positive_finite(size), "confidence": float(confidence or 0.0),
    }
    try:
        result = admission.evaluate(inst)
    except Exception as exc:
        out["state"] = "not_assessed_admission_error"
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("executability: admission.evaluate failed for %s: %s",
                       symbol, exc, exc_info=True)
        return out

    unknowns: list = list(_as_dict(cost).get("unknown_items") or [])
    screening_missing = list(_as_dict(screening).get("missing_fields") or [])
    for field in screening_missing:
        if field not in unknowns:
            unknowns.append(field)
    out.update({
        "state": "assessed",
        "verdict": result.get("verdict"),
        "reasons": list(result.get("reasons") or []),
        "metrics": dict(result.get("metrics") or {}),
        "enforce_at_execution": bool(result.get("enforce")),
        "would_block_if_enforced": bool(result.get("verdict") == "REJECT" and result.get("enforce")),
        "unknowns": unknowns,
    })
    return out


def build_planned_executability(*, preview: Optional[Dict[str, Any]], sizing_ctx: Dict[str, Any],
                                symbol: str, direction: str, entry: Any, stop_loss: Any,
                                take_profit: Any, confidence: float = 0.0,
                                horizon_hours: Any = None, trade_date: Optional[str] = None,
                                screening: Optional[Dict[str, Any]] = None,
                                borrow_rates: Optional[Dict[str, Dict[str, Any]]] = None,
                                regime: Optional[str] = None) -> Dict[str, Any]:
    """三段合一 → 合入 `planned_preview` 的附加键 (不与 P3.1 的键冲突)。

    永不抛: 任何异常退化成显式 `exec_state='not_generated_*'` —— 预览缺数不该让注入失败,
    但也不能靠"键不存在"表态 (§17.3-② 同型歧义)。
    """
    ctx = dict(_as_dict(sizing_ctx))
    screening = _as_dict(screening)
    preview = _as_dict(preview)
    if regime is not None:
        ctx["regime"] = regime
    elif "regime" not in ctx:
        ctx["regime"] = screening.get("market_regime")
    base = {
        "exec_basis_version": PLANNED_EXEC_BASIS_VERSION,
        "exec_state": "not_generated",
        "sizing_context": {
            "state": ctx.get("state"), "equity": _round(ctx.get("equity"), 2),
            "equity_origin": ctx.get("equity_origin"), "equity_asof": ctx.get("equity_asof"),
            "equity_age_hours": ctx.get("equity_age_hours"),
            "equity_stale": ctx.get("equity_stale"),
            "water_level": ctx.get("water_level"), "water_mult": ctx.get("water_mult"),
            "risk_pct": ctx.get("risk_pct"), "max_position_pct": ctx.get("max_position_pct"),
            "trade_env": ctx.get("trade_env"), "notes": list(ctx.get("notes") or []),
        },
    }
    try:
        if preview.get("state") != "generated":
            base["exec_state"] = "not_generated_no_preview"
            return base
        size_block = estimate_size(entry=entry, stop_loss=stop_loss, direction=direction,
                                   ctx=ctx, confidence=confidence)
        cost_block = estimate_cost(entry=entry, take_profit=take_profit,
                                   size=size_block.get("size"), direction=direction,
                                   symbol=symbol, horizon_hours=horizon_hours,
                                   borrow_rates=borrow_rates, screening=screening,
                                   trade_date=trade_date)
        exec_block = assess_executability(symbol=symbol, direction=direction, entry=entry,
                                          stop_loss=stop_loss, take_profit=take_profit,
                                          size=size_block.get("size"), confidence=confidence,
                                          trade_date=trade_date, screening=screening,
                                          cost=cost_block)
        base.update({
            "exec_state": "generated",
            "size_estimate": size_block,
            "cost_coverage": cost_block,
            "executability": exec_block,
        })
        return base
    except Exception as exc:
        base["exec_state"] = "not_generated_error"
        base["exec_error"] = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("planned executability failed for %s: %s", symbol, exc, exc_info=True)
        return base
