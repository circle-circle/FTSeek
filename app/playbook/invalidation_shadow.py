"""
invalidation 影子触发 (Stage 2a, 2026-07-11) — 退出引擎优化 §工作块D

目标: 让 DA 的 thesis 失效条件 (invalidation) 在退出侧"影子运行" —— 记录"若按
invalidation 离场会何时触发", 与实际退出对比累积样本, 为未来实盘灰度提供证据。
**当前不实平** (纯观测)。

现状约束: invalidation 多为自然语言 (如"跌破关键支撑 490" / "基本面恶化")。机器只能判定
其中含明确价格/百分比阈值的子集; 其余标记 unstructured, 待 Hermes 侧结构化 (Stage 2b)。

Stage 2b 建议契约 (Hermes 侧未来产出, 便于全自动判定):
    invalidation_rules: [
      {"type": "price_below" | "price_above" | "pct_drop" | "pct_gain",
       "value": <float>, "note": <str>}
    ]
evaluate_invalidation_shadow() 同时接受结构化 rules 与自然语言文本 (尽力解析)。
"""
import re

# 自然语言尽力解析: 价格阈值 / 百分比阈值
_PRICE_PAT = re.compile(
    r"(?:跌破|破位|跌穿|下破|below|支撑[位]?)\s*[:：]?\s*\$?(\d+(?:\.\d+)?)")
_PCT_PAT = re.compile(r"(?:回撤|下跌|跌|drop)\s*(\d+(?:\.\d+)?)\s*%")


def _eval_rule(rule, direction, entry_price, current_price):
    """判定单条结构化规则, 返回 (hit, why)。"""
    t = rule.get("type")
    v = rule.get("value")
    if v is None or current_price is None:
        return (False, "")
    if t == "price_below":
        return (current_price <= v, f"价格 {current_price} ≤ {v}")
    if t == "price_above":
        return (current_price >= v, f"价格 {current_price} ≥ {v}")
    if t in ("pct_drop", "pct_gain") and entry_price:
        chg = (current_price - entry_price) / entry_price * 100
        adverse = -chg if direction == "LONG" else chg
        return (adverse >= v, f"不利变动 {adverse:.1f}% ≥ {v}%")
    return (False, "")


def evaluate_invalidation_shadow(*, invalidation, direction, entry_price,
                                 current_price, structured_rules=None):
    """影子评估: DA 的 invalidation 此刻是否会触发离场。

    纯函数, 无副作用。返回 dict:
      would_trigger: bool  — 此刻是否满足失效条件
      reason:        str   — 判定说明
      parseable:     bool  — 条件是否机器可判定 (False = 自然语言待结构化)
    """
    direction = (direction or "LONG").upper()

    # 1. 结构化规则优先 (Stage 2b 落地后走这条)
    if structured_rules:
        for rule in structured_rules:
            hit, why = _eval_rule(rule, direction, entry_price, current_price)
            if hit:
                return {"would_trigger": True, "reason": why, "parseable": True}
        return {"would_trigger": False, "reason": "结构化规则未命中", "parseable": True}

    # 2. 自然语言尽力解析 (Stage 2a 过渡)
    text = (invalidation or "").strip()
    if not text:
        return {"would_trigger": False, "reason": "无 invalidation", "parseable": False}

    m = _PRICE_PAT.search(text)
    if m and entry_price and current_price:
        thr = float(m.group(1))
        if direction == "LONG" and current_price <= thr:
            return {"would_trigger": True,
                    "reason": f"价格 {current_price} 跌破阈值 {thr}", "parseable": True}
        if direction == "SHORT" and current_price >= thr:
            return {"would_trigger": True,
                    "reason": f"价格 {current_price} 突破阈值 {thr}", "parseable": True}
        return {"would_trigger": False, "reason": f"价格阈值 {thr} 未触及", "parseable": True}

    mp = _PCT_PAT.search(text)
    if mp and entry_price and current_price:
        pct = float(mp.group(1))
        chg = (current_price - entry_price) / entry_price * 100
        adverse = -chg if direction == "LONG" else chg
        if adverse >= pct:
            return {"would_trigger": True,
                    "reason": f"不利变动 {adverse:.1f}% ≥ {pct}%", "parseable": True}
        return {"would_trigger": False, "reason": f"不利变动未达 {pct}%", "parseable": True}

    return {"would_trigger": False,
            "reason": "invalidation 为自然语言, 待结构化 (Stage 2b)", "parseable": False}


if __name__ == "__main__":
    # 自测: 覆盖结构化 / 价格文本 / 百分比文本 / 未命中 / 不可解析
    _cases = [
        (dict(invalidation="跌破关键支撑 490", direction="LONG",
              entry_price=500, current_price=489), True, True),
        (dict(invalidation="跌破关键支撑 490", direction="LONG",
              entry_price=500, current_price=495), False, True),
        (dict(invalidation="回撤 3% 止损", direction="LONG",
              entry_price=100, current_price=96), True, True),
        (dict(invalidation="基本面恶化则离场", direction="LONG",
              entry_price=100, current_price=90), False, False),
        (dict(invalidation="", direction="LONG",
              entry_price=100, current_price=90), False, False),
        (dict(invalidation="x", direction="LONG", entry_price=100, current_price=90,
              structured_rules=[{"type": "price_below", "value": 95}]), True, True),
    ]
    _ok = 0
    for kw, exp_trig, exp_parse in _cases:
        r = evaluate_invalidation_shadow(**kw)
        good = r["would_trigger"] == exp_trig and r["parseable"] == exp_parse
        _ok += good
        print(f"  [{'OK' if good else '✗'}] trig={r['would_trigger']} parse={r['parseable']} | {r['reason']}")
    print(f"\n{_ok}/{len(_cases)} passed")
