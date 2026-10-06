"""指令准入门 (admission gate) — 全写入方统一收口 (2026-08-06).

立项根因 (源码 + 实证, DEB-20260806-source-control):
  1. 门只作用于一条路径: confidence 门 (BUY 70 / SHORT 65) 写在
     `_build_instructions_from_votes` 内 (pipeline.py:1264), 但 DA 候选由外部
     deep_analysis skill **直写 DB** (pipeline.py:1523-1532 明确记载), UE 由
     `scripts/llm_universe_scan.py:1233` 直写 → 两者绕过 confidence 门、
     负 edge 配对门、价格偏离门、跨日翻转降仓。
     实测 35 日: DA 219 条 BUY 平均 conf=36 全部入库, 执行 123 笔。
  2. 费用地板是结构性漏损: commission = max(min($0.01×qty, 0.3%×amt), $1.99)/腿。
     qty≤199 时恒付 $1.99 地板 → fee_bps ≈ 2×1.99/notional。
     实测 fee 中位 $4.2/往返, 名义额中位 $3.4k → 12.9bps, 而毛边际 ≈ −2bps。

设计原则:
  - 纯函数 + 一次性读配置, 零 IO, 可单测 (同 fee_model.py 风格);
  - 代码 fallback 为 record-only (`_DEFAULTS.enforce=false`), 配置不可用时不改指令;
  - 生产 YAML 当前 `enabled=true/enforce=true`, 命中已启用的费用/价格门会硬拦;
  - 收口点选 `PlaybookDB.save_instruction` (db.py:124) — 唯一全写入方必经处,
    DA 直写/UE/topup/generate 四条路径一次覆盖;
  - 回滚: playbook.admission.enabled=false (或删除 save_instruction 内调用)。

口径警示 (不可夸大):
  - 费用类判据是**算术确定**的 (fee_model 同源), 与预测无关;
  - confidence 类判据**无实证支持**: 60 日 n=242 实测 confidence IC=−0.021
    (t=−0.33), 全部事前特征 |t|<1 → 默认阈值设为 0 (不拦), 仅记录。
    任何启用都必须先有 IC 转正证据 (见 development_practice_specification
    "期望值替代confidence" 规范)。

异常值语义 (2026-08-06 R1 复核建议 2 补记):
  `_f()` 将 None/False/空字符串统一视为 0; 若上游传入 NaN, 后续比较恒为 False
  → **当前判据静默跳过, 不作为拦截条件** (宁漏不误杀: 异常值不应导致指令被拦)。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# ── LIVE 工程探针豁免 (2026-10-06, 用户裁「1只股真实试运行」) ─────────────
# 只豁免**经济性**判据; 安全性判据 (SL_STOP_TOO_WIDE / PRICE_TOO_LOW /
# SHARES_ABOVE_FEE_KNEE) 一律保留 — 探针也必须有止损卫生与价格地板。
#
# 为什么必须豁免: `max_order_qty=1` 是 RFC-131 C-lite 的**硬要求**
# (live_arm_gate.py:122 `max_order_qty != 1` → LIVE_PROBE_LIMIT_INVALID),
# 而 notional = 1 × 股价 恒 < min_notional_usd=2500 ⇒ 门恒 REJECT ⇒
# 探针结构性不可执行 (美股最高价股 notional≈$1,200 仍不足)。
# 实证 2026-10-05: US.GEO/US.ORCL 镜像行被本门改写 SKIP
# (skip_reason=NOTIONAL_TOO_SMALL(33<2500)), 随后死于「指令不存在」。
#
# 为什么豁免不违背本门立项理由: 名义额地板的存在理由是费用地板
# (docstring 根因 2: qty≤199 恒付 $1.99/腿), 那是**盈利性**约束;
# 探针不以盈利为目的, 只验证 SIM→LIVE 全链路 (含券商 STOP 台账落库),
# 故豁免经济性门不损其目的。该笔 fee_bps 会很高 (GEO 1股≈1231bp),
# 属预期 — 它是链路验证成本, 不得计入策略经济性样本。
#
# 无需独立开关: 探针关闭 (live_manual_arm.enabled=false / probe.authorized=false /
# HALT / ALLOW_REAL_TRADING=false) 后不再生成镜像行, 本豁免自动失效 (惰性)。
_PROBE_EXEMPT_REASONS = frozenset({"NOTIONAL_TOO_SMALL"})


def _is_live_engineering_probe(inst: Dict[str, Any]) -> bool:
    """该指令是否为 LIVE 工程探针镜像行 (标记由 pipeline._mirror_instruction_to_live 写入).

    只认双条件 target_env∈{real,live} ∧ mode=="engineering_probe" — SIM 行
    无此标记, 故 SIM 臂判定完全不受影响 (不放宽任何生产门)。
    """
    md = inst.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (TypeError, ValueError):
            return False
    if not isinstance(md, dict):
        return False
    dual = md.get("dual_arm")
    if not isinstance(dual, dict):
        return False
    return (str(dual.get("target_env") or "").strip().lower() in ("real", "live")
            and str(dual.get("mode") or "").strip() == "engineering_probe")

# ── 内置缺省 (可被 unified_config.yaml `playbook.admission` 覆盖) ──
_DEFAULTS: Dict[str, Any] = {
    "enabled": True,          # 判定+审计总开关
    "enforce": False,         # false=record-only (不改指令); true=硬拦 (转 SKIP)
    # ── 费用效率类 (算术确定, 可安全 enforce) ──
    # 名义额下限: fee_bps ≈ 2×1.99/notional; $3k → 13bps, $8k → 5bps
    "min_notional_usd": 0.0,          # 0=不拦 (先观测分布再定档)
    # 低价股: commission 撞 0.3%×amt 上限 → 单腿最高 30bps
    "min_entry_price": 0.0,           # 0=不拦; 建议档 20~30
    # 每腿佣金地板 $1.99 在 qty=199 处与 $0.01/股 等价; 超过即线性加价
    "max_shares_per_order": 0,        # 0=不拦
    # 费用效率: TP 处毛利 ≥ K × 往返费用 (K 越大越严)
    "min_profit_fee_ratio": 0.0,      # 0=不拦; 建议档 6~10
    # ── confidence 类 (无 IC 证据, 默认全不拦) ──
    "min_confidence_buy": 0,
    "min_confidence_short": 0,
    # ── 结构卫生 (2026-08-07 用户哲学"留空间/不强不做") ──
    # 止损宽度: 距入场 >max_sl_pct% = 单笔最大亏损失控 (静态 R:R 不可靠 —
    #   止盈端常被保本/锁盈/EOD 替代, 真正硬约束是止损端), 0=不拦
    "max_sl_pct": 0.0,
}

_cfg_cache: Optional[Dict[str, Any]] = None


def get_admission_config(refresh: bool = False) -> Dict[str, Any]:
    """配置: `playbook.admission` 覆盖内置缺省 (浅合并, 缺键回退)。"""
    global _cfg_cache
    if _cfg_cache is not None and not refresh:
        return _cfg_cache
    cfg = dict(_DEFAULTS)
    try:
        from app.core.config_container import get_config
        override = get_config().get_dotted("playbook.admission", None)
        if isinstance(override, dict):
            for k, v in override.items():
                if k in cfg and v is not None:
                    cfg[k] = v
    except Exception:
        pass  # 配置容器不可用 (单测/脚本) → 内置缺省
    _cfg_cache = cfg
    return cfg


def reset_cache() -> None:
    """热重载/单测用: 丢弃配置缓存。"""
    global _cfg_cache
    _cfg_cache = None


def estimate_round_trip_fee(qty: float, entry_price: float,
                            direction: str, trade_date: Optional[str] = None) -> float:
    """往返费用估算 — 复用 fee_model (唯一真源), 失败时退化为地板近似。

    退化值 $3.98 = 2×commission_min_per_order($1.99), 仅在 fee_model 不可用时使用。
    """
    try:
        from app.execution.fee_model import estimate_round_trip
        return float(estimate_round_trip(
            qty, entry_price, entry_price, direction, trade_date
        )["total"])
    except Exception:
        return 3.98


def evaluate(inst: Dict[str, Any]) -> Dict[str, Any]:
    """判定单条指令是否准入。纯函数, 不改入参。

    Args:
        inst: save_instruction 期望的指令 dict (date/symbol/direction/entry_price/
              take_profit/position_size/confidence ...)

    Returns:
        {
          "verdict": "PASS" | "REJECT",
          "reasons": [str, ...],        # 命中的判据代码, 空=无命中
          "metrics": {...},             # 可审计中间量 (fee/notional/fee_bps/...)
          "enforce": bool,              # 当前是否硬拦模式
        }
    """
    cfg = get_admission_config()
    direction = str(inst.get("direction") or "").upper()

    metrics: Dict[str, Any] = {}
    reasons: List[str] = []

    # 只判可执行方向; SKIP/未知方向直接放行 (它们本就不下单)
    if direction not in ("BUY", "SELL_SHORT"):
        return {"verdict": "PASS", "reasons": [], "exempted_reasons": [],
                "metrics": metrics,
                "enforce": bool(cfg.get("enforce", False))}

    entry_price = _f(inst.get("entry_price"))
    take_profit = _f(inst.get("take_profit"))
    qty = _f(inst.get("position_size"))
    confidence = _f(inst.get("confidence"))

    notional = entry_price * qty
    metrics["notional"] = round(notional, 2)
    metrics["shares"] = qty
    metrics["entry_price"] = entry_price

    if qty > 0 and entry_price > 0:
        fee = estimate_round_trip_fee(qty, entry_price, direction, inst.get("date"))
        metrics["rt_fee"] = round(fee, 2)
        metrics["fee_bps"] = round(10000.0 * fee / notional, 1) if notional > 0 else None
        # TP 处毛利 (方向自适应; 无 TP 时不判 ratio)
        if take_profit > 0:
            gross_at_tp = abs(take_profit - entry_price) * qty
            metrics["gross_at_tp"] = round(gross_at_tp, 2)
            ratio = gross_at_tp / fee if fee > 0 else None
            metrics["profit_fee_ratio"] = round(ratio, 2) if ratio is not None else None
            _min_ratio = _f(cfg.get("min_profit_fee_ratio"))
            if _min_ratio > 0 and ratio is not None and ratio < _min_ratio:
                reasons.append(f"FEE_INEFFICIENT(ratio={ratio:.1f}<{_min_ratio:g})")

    # 名义额下限 → fee_bps 过高
    _min_nt = _f(cfg.get("min_notional_usd"))
    if _min_nt > 0 and 0 < notional < _min_nt:
        reasons.append(f"NOTIONAL_TOO_SMALL({notional:.0f}<{_min_nt:g})")

    # 止损宽度卫生 (2026-08-07 用户哲学"留空间"): 止损距入场过宽 = 单笔最大亏损失控。
    #   替代静态 R:R 门 — 止盈端 (R:R 分子) 常被保本/锁盈/EOD 退出替代, 不可靠;
    #   止损端 (分母) 是硬约束, 永远有效。0=不拦。
    _sl = _f(inst.get("stop_loss"))
    _sl_max = _f(cfg.get("max_sl_pct"))
    if _sl_max > 0 and entry_price > 0 and _sl > 0:
        _sl_pct = abs(entry_price - _sl) / entry_price * 100.0
        if _sl_pct > _sl_max:
            reasons.append(f"SL_STOP_TOO_WIDE({_sl_pct:.1f}%>{_sl_max:g}%)")

    # 弱信号审计标记 (2026-08-07 用户哲学"不强不做"): 辩论一方弃权 = 弱信号。
    #   record-only (不强拦): sim 采集期供给优先 + confidence 类无 IC 证据;
    #   攒 n≥30 弃权组实际表现再决定硬拦/反向。方向冲突已在 da_gate 决策环节拦截。
    _rawi = inst.get("raw_instruction")
    if isinstance(_rawi, dict) and "弃权" in str(_rawi.get("debate_outcome") or ""):
        metrics["weak_signal"] = True
        metrics["weak_reason"] = str(_rawi.get("debate_outcome"))[:80]

    # 低价股 → commission 撞 0.3%×amt 上限
    _min_px = _f(cfg.get("min_entry_price"))
    if _min_px > 0 and 0 < entry_price < _min_px:
        reasons.append(f"PRICE_TOO_LOW({entry_price:.2f}<{_min_px:g})")

    # 股数上限 → 越过 $1.99 地板等价点后佣金线性加价
    _max_sh = _f(cfg.get("max_shares_per_order"))
    if _max_sh > 0 and qty > _max_sh:
        reasons.append(f"SHARES_ABOVE_FEE_KNEE({qty:.0f}>{_max_sh:g})")

    # confidence 类 (默认阈值 0 = 不拦; 无 IC 证据前不应启用)
    _min_conf = _f(cfg.get("min_confidence_buy") if direction == "BUY"
                   else cfg.get("min_confidence_short"))
    if _min_conf > 0 and confidence < _min_conf:
        reasons.append(f"LOW_CONFIDENCE({confidence:.0f}<{_min_conf:g})")

    # LIVE 工程探针: 豁免经济性判据 (见模块头 _PROBE_EXEMPT_REASONS 注释).
    # 安全性判据保留 — 若探针同时命中 NOTIONAL_TOO_SMALL 与 SL_STOP_TOO_WIDE,
    # 仍 REJECT (只是理由里少了经济性那条)。豁免事实写进 metrics 供审计。
    _exempted: List[str] = []
    if reasons and _is_live_engineering_probe(inst):
        _exempted = [r for r in reasons
                     if r.split("(", 1)[0] in _PROBE_EXEMPT_REASONS]
        if _exempted:
            reasons = [r for r in reasons if r not in _exempted]
            metrics["probe_exemption"] = {
                "exempted_reasons": _exempted,
                "mode": "engineering_probe",
                "notional": metrics.get("notional"),
                "fee_bps": metrics.get("fee_bps"),
                "note": "经济性门对 LIVE 1股探针豁免; 本笔不得计入策略经济性样本",
            }

    return {
        "verdict": "REJECT" if reasons else "PASS",
        "reasons": reasons,
        "exempted_reasons": _exempted,
        "metrics": metrics,
        "enforce": bool(cfg.get("enforce", False)),
    }


def apply(inst: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """对指令施加准入门, 返回 (可能改写后的 inst, 判定结果)。

    record-only (enforce=false): 只把判定写入 raw_instruction.admission, 方向不变。
    enforce=true 且 REJECT: 方向改 SKIP + skip_reason, 原方向留存于 admission 审计,
      使"被拦下的指令"仍可归因 (与既有 SKIP 行同轨, 不丢样本)。
    """
    cfg = get_admission_config()
    if not bool(cfg.get("enabled", True)):
        return inst, {"verdict": "PASS", "reasons": [], "metrics": {}, "enforce": False}

    res = evaluate(inst)

    # 审计落库: 挂在 raw_instruction 下 (save_instruction 会序列化为 raw_instruction_json)
    #   注意: 部分写入方可能把 raw_instruction 存成非 dict (如 LLM 原始文本) —
    #   此时**不可直接替换**, 否则丢原始输出; 改为包装保留于 _original。
    raw = inst.get("raw_instruction")
    if not isinstance(raw, dict):
        raw = {"_original": raw} if raw is not None else {}
        inst["raw_instruction"] = raw
    raw["admission"] = {
        "verdict": res["verdict"],
        "reasons": res["reasons"],
        "metrics": res["metrics"],
        "enforced": bool(res["enforce"]),
    }

    if res["verdict"] == "REJECT" and res["enforce"]:
        raw["admission"]["original_direction"] = inst.get("direction")
        inst["direction"] = "SKIP"
        _why = "; ".join(res["reasons"])
        inst["skip_reason"] = f"admission: {_why}"

    return inst, res


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0
