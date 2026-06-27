"""
LLM Validator — Stage 2 of Battle Plan Pipeline

对Factor Engine的Top N候选股逐一进行LLM深度验证。
每个候选1次LLM调用，并行执行，结构化输出。

输入: factor_screen candidates + 技术指标 + 板块联动 + 历史交易
输出: verdict/entry_zone/stop_loss/take_profit/execution_rules/factor_validation

设计原则:
- temperature=0 (确定性)
- json_mode=True (强制JSON)
- max_tokens=500 (够结构化但不给废话空间)
- 每symbol每日最多1次 (DB缓存)
"""

import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek_us.plan_validator")

DB_PATH = Path(__file__).parent.parent.parent / "data" / "ftseek_us_live.db"

SYSTEM_PROMPT = """你是一个严苛的量化交易审计师。量化系统选出了一只候选股并给出了5因子评分。
你的任务不是预测涨跌，而是:

1. 验证每个因子是否可靠 (例如: 量价突破但板块下跌=假突破)
2. 给出明确的入场区间、止损、止盈 (引用ATR和支撑阻力位)
3. 定义INVALIDATION条件 (什么情况下setup失效)
4. 给出执行规则 (跳空高开/低开怎么办)
5. 评估CONFIDENCE (0-100)

## 判定规则 (必须严格遵守)
- 因子验证中 ≥3个CONFIRMED → **verdict=VALID** (即使有1-2个FALSE_POSITIVE)
- 因子验证中 ≥3个WEAK或FALSE_POSITIVE → verdict=INVALID
- 2个CONFIRMED + 2个WEAK + 1个FALSE_POSITIVE → verdict=CONDITIONAL

**重要**: volume_breakout因子容易被判为FALSE_POSITIVE (因为量比<1.0x很常见)。
如果其他4个因子中有3个CONFIRMED，即使volume_breakout是FALSE_POSITIVE，仍然应该给VALID。
将量价疑虑写入audit_flags和降低confidence，但不要因为缩量就拒绝VALID。

## 输出格式 (严格JSON)
{
  "verdict": "VALID" | "INVALID" | "CONDITIONAL",
  "direction": "LONG" | "SHORT",
  "confidence": 0-100,
  "entry_zone": [price_low, price_high],
  "stop_loss": price,
  "take_profit": price,
  "risk_reward": float,
  "position_size_pct": 3-8,
  "invalidation": "具体条件",
  "trigger_condition": "入场触发信号",
  "execution_rules": {
    "gap_up_over_2pct": "SKIP" | "CHASE_SL_AT_OPEN",
    "gap_down_over_2pct": "SKIP" | "WAIT_RECOVERY",
    "hit_zone_then_drop": "WAIT_5MIN_CANDLE_ABOVE"
  },
  "factor_validation": {
    "sector_momentum": "CONFIRMED" | "WEAK" | "FALSE_POSITIVE",
    "relative_strength": "CONFIRMED" | "WEAK" | "FALSE_POSITIVE",
    "ma_alignment": "CONFIRMED" | "WEAK" | "FALSE_POSITIVE",
    "volume_breakout": "CONFIRMED" | "WEAK" | "FALSE_POSITIVE",
    "rsi_sweet_spot": "CONFIRMED" | "WEAK" | "FALSE_POSITIVE"
  },
  "audit_flags": ["简短风险点1", "简短风险点2"]
}

要求:
- entry_zone必须引用支撑/阻力位或均线，不是随意数字
- stop_loss必须基于ATR (1.5-2x ATR from entry)
- 不要输出推理过程，只输出JSON"""


def _build_candidate_prompt(candidate: Dict, context: Dict) -> str:
    """为单个候选股构建LLM prompt"""
    sym = candidate["symbol"]
    tech = candidate.get("technicals", {})
    factors = candidate.get("factors", {})
    sectors = candidate.get("sectors", [])

    prompt = f"""## 候选: {sym}
## 综合因子分数: {candidate['composite_score']:.3f} (方向建议: {candidate['direction_hint']})

## 因子分解
"""
    for fname, fdata in factors.items():
        status = "✅" if fdata["score"] >= 0.7 else "⚠️" if fdata["score"] >= 0.4 else "❌"
        prompt += f"- {status} {fname}: score={fdata['score']:.2f} (weight={fdata['weight']:.0%}) — {fdata.get('detail', '')}\n"

    prompt += f"""
## 技术指标
- 现价: ${tech.get('cur_price', 0):.2f}
- MA5={tech.get('ma5', 0):.0f} / MA10={tech.get('ma10', 0):.0f} / MA20={tech.get('ma20', 0):.0f}
- RSI14={tech.get('rsi14', 50):.0f}
- 量比={tech.get('vol_ratio', 1):.1f}x
- 20日涨跌: {tech.get('change_20d', 0):+.1f}%
- 5日涨跌: {tech.get('change_5d', 0):+.1f}%
- 20日高点=${tech.get('high_20d', 0):.2f} / 低点=${tech.get('low_20d', 0):.2f}

## 市场背景
- Regime: {context.get('regime', 'NEUTRAL')}
- SPY 20日涨跌: {context.get('spy_change_20d', 0):+.1f}%
"""

    # Catalyst context (from Stage 1.5, v2: 10分制)
    catalyst = candidate.get("catalyst", {})
    if catalyst and catalyst.get("type") and catalyst.get("type") != "NONE":
        cat_score = catalyst.get('score', 0)
        prompt += f"\n## 催化剂 (强度 {cat_score}/10)\n"
        prompt += f"- 类型: {catalyst.get('type', 'N/A')}\n"
        prompt += f"- 强度分: {cat_score}/10 ({catalyst.get('strength', 'N/A')})\n"
        prompt += f"- 标题: {catalyst.get('headline', 'N/A')}\n"
        if catalyst.get('price_change_pct'):
            prompt += f"- 价格确认: 已移动 {catalyst['price_change_pct']:+.1f}% (催化剂已被市场反映)\n"
        if catalyst.get('source_count', 0) > 1:
            prompt += f"- 多源确认: {catalyst['source_count']}条新闻命中同一催化剂\n"
        if catalyst.get('direction_hint'):
            prompt += f"- 催化剂方向暗示: {catalyst['direction_hint']}\n"

    # Discovery source context
    discovery_source = candidate.get("discovery_source", "")
    if discovery_source == "event_discovery":
        prompt += "\n- 发现来源: Finnhub异动发现 (核心池外新发现, 需更严格验证)\n"

    # Sector peers
    sector_momentum = context.get("sector_momentum", {})
    if sectors:
        prompt += "\n## 所属板块\n"
        for sec in sectors:
            chg = sector_momentum.get(sec, 0)
            prompt += f"- {sec}: {chg:+.1f}%\n"

    # Peer changes
    peer_changes = context.get("peer_changes", {}).get(sym, {})
    if peer_changes:
        prompt += "\n## 板块peers今日涨跌\n"
        for peer, chg in peer_changes.items():
            prompt += f"- {peer}: {chg:+.1f}%\n"

    # Historical trades
    hist = context.get("trade_history", {}).get(sym)
    if hist and hist.get("count", 0) > 0:
        prompt += f"\n## 该symbol历史交易\n"
        prompt += f"- 总次数: {hist['count']}, 胜率: {hist['win_rate']:.0f}%, 总盈亏: ${hist['total_pnl']:.2f}\n"
        for t in hist.get("last_trades", [])[:3]:
            prompt += f"- {t['date']} {t['direction']}: ${t['pnl']:+.2f}\n"

    # Bear cases from previous plan
    bear = context.get("bear_cases", {}).get(sym)
    if bear:
        prompt += f"\n## 空头审计师质疑\n{bear}\n"

    return prompt


def _validate_single_deterministic(candidate: Dict, context: Dict) -> Dict:
    """P0 (2026-06-17): 确定性规则引擎验证 — 替代 LLM 调用。

    将原 LLM prompt 中用自然语言写清的判定规则「编译」为确定性代码。
    仅当判定为 CONDITIONAL (边界案例) 时才降级到 LLM 做深度判断。

    规则来源: 原 SYSTEM_PROMPT 中的判定规则 + 实盘数据校准。
    """
    sym = candidate["symbol"]
    factors = candidate.get("factors", {})
    tech = candidate.get("technicals", {})
    catalyst = candidate.get("catalyst", {})
    composite = candidate.get("composite_score", 0)
    direction_hint = candidate.get("direction_hint", "LONG")

    # ── Step 1: 因子验证 (逐因子判定 CONFIRMED / WEAK / FALSE_POSITIVE) ──
    factor_validation = {}
    for fname, fdata in factors.items():
        score = fdata.get("score", 0)
        weight = fdata.get("weight", 0)
        detail = fdata.get("detail", "")

        if fname == "volume_breakout":
            # 特殊规则: volume_breakout 容易被误判 (原 prompt 明确说明)
            # 量比 < 1.0 不直接判 FALSE_POSITIVE, 给 WEAK
            vol_ratio = tech.get("vol_ratio", 1.0)
            if score >= 0.7 and vol_ratio >= 1.2:
                factor_validation[fname] = "CONFIRMED"
            elif score >= 0.4 or vol_ratio >= 0.8:
                factor_validation[fname] = "WEAK"
            else:
                factor_validation[fname] = "FALSE_POSITIVE"
        elif fname == "sector_momentum":
            # 板块动量: 需要板块实际涨跌确认 (方向感知)
            sectors = candidate.get("sectors", [])
            sector_momentum = context.get("sector_momentum", {})
            sector_chgs = [sector_momentum.get(s, 0) for s in sectors if s in sector_momentum]
            avg_sector_chg = sum(sector_chgs) / len(sector_chgs) if sector_chgs else 0
            if direction_hint == "SHORT":
                # SHORT: 板块跌 = 做空确认
                if score >= 0.6 and avg_sector_chg < 0:
                    factor_validation[fname] = "CONFIRMED"
                elif score >= 0.3 or avg_sector_chg < 0.5:
                    factor_validation[fname] = "WEAK"
                else:
                    factor_validation[fname] = "FALSE_POSITIVE"
            else:
                if score >= 0.6 and avg_sector_chg > 0:
                    factor_validation[fname] = "CONFIRMED"
                elif score >= 0.3 or avg_sector_chg > -0.5:
                    factor_validation[fname] = "WEAK"
                else:
                    factor_validation[fname] = "FALSE_POSITIVE"
        elif fname == "relative_strength":
            # 相对强度: 与 SPY 对比 (方向感知)
            spy_chg = context.get("spy_change_20d", 0)
            stock_chg = tech.get("change_20d", 0)
            if direction_hint == "SHORT":
                # SHORT: 跑输SPY = 做空确认
                if score >= 0.6 and stock_chg < spy_chg:
                    factor_validation[fname] = "CONFIRMED"
                elif score >= 0.3:
                    factor_validation[fname] = "WEAK"
                else:
                    factor_validation[fname] = "FALSE_POSITIVE"
            else:
                # LONG: 跑赢SPY = 做多确认
                if score >= 0.6 and stock_chg > spy_chg:
                    factor_validation[fname] = "CONFIRMED"
                elif score >= 0.3:
                    factor_validation[fname] = "WEAK"
                else:
                    factor_validation[fname] = "FALSE_POSITIVE"
        else:
            # 通用规则: ma_alignment, rsi_sweet_spot 等
            if score >= 0.7:
                factor_validation[fname] = "CONFIRMED"
            elif score >= 0.4:
                factor_validation[fname] = "WEAK"
            else:
                factor_validation[fname] = "FALSE_POSITIVE"

    # ── Step 2: 统计因子判定结果 ──
    confirmed_count = sum(1 for v in factor_validation.values() if v == "CONFIRMED")
    weak_count = sum(1 for v in factor_validation.values() if v == "WEAK")
    fp_count = sum(1 for v in factor_validation.values() if v == "FALSE_POSITIVE")
    total_factors = len(factor_validation)

    # ── Step 3: 判定 verdict (原 prompt 规则的代码化) ──
    # 规则 1: ≥3 CONFIRMED → VALID (即使有 1-2 个 FALSE_POSITIVE)
    # 特殊: volume_breakout FP 不阻止 VALID (原 prompt 明确说明)
    non_vol_fp = sum(1 for k, v in factor_validation.items()
                     if v == "FALSE_POSITIVE" and k != "volume_breakout")

    if confirmed_count >= 3:
        verdict = "VALID"
        confidence = min(95, 55 + confirmed_count * 10)
        # volume_breakout FP → 降 confidence 但不阻止 VALID
        if factor_validation.get("volume_breakout") == "FALSE_POSITIVE":
            confidence = max(40, confidence - 15)
    # 规则 2: ≥3 WEAK 或 FALSE_POSITIVE → INVALID
    elif weak_count + fp_count >= 3:
        verdict = "INVALID"
        confidence = max(10, 70 - (weak_count + fp_count) * 10)
    # 规则 3: 2 CONFIRMED + 强催化剂 → VALID
    elif confirmed_count >= 2 and catalyst.get("score", 0) >= 7:
        verdict = "VALID"
        confidence = min(85, 50 + catalyst.get("score", 0) * 4)
    # 规则 4: 2 CONFIRMED → CONDITIONAL (边界案例 → 可走 LLM)
    elif confirmed_count >= 2:
        verdict = "CONDITIONAL"
        confidence = min(70, 40 + confirmed_count * 10)
    # 规则 5: 高 composite + 催化剂 → CONDITIONAL
    elif composite >= 0.6 and catalyst.get("score", 0) >= 5:
        verdict = "CONDITIONAL"
        confidence = 45
    # 默认: INVALID
    else:
        verdict = "INVALID"
        confidence = max(10, 30 + int(composite * 30))

    # ── Step 4: 历史交易惩罚 ──
    hist = context.get("trade_history", {}).get(sym)
    if hist and hist.get("count", 0) >= 3:
        wr = hist.get("win_rate", 50)
        total_pnl = hist.get("total_pnl", 0)
        if wr < 25 and total_pnl < -200:
            confidence = max(10, confidence - 15)
            if verdict == "VALID":
                verdict = "CONDITIONAL"

    # ── Step 5: Regime 调整 (方向感知) ──
    regime = context.get("regime", "NEUTRAL")
    if regime in ("RISK_OFF", "HIGH_VOL"):
        if direction_hint == "LONG":
            confidence = max(10, confidence - 10)
        elif direction_hint == "SHORT":
            confidence = min(95, confidence + 5)  # SHORT在避险市占优

    # ── Step 6: 入场/止损/止盈 (从技术指标计算, 方向感知) ──
    cur_price = tech.get("cur_price", 0)
    atr = tech.get("atr14", cur_price * 0.02 if cur_price > 0 else 1)
    if cur_price > 0:
        if direction_hint == "SHORT":
            # SHORT: entry near current, stop above, profit below
            entry_zone = [round(cur_price - atr * 0.3, 2), round(cur_price + atr * 0.5, 2)]
            stop_loss = round(cur_price + atr * 1.8, 2)
            take_profit = round(cur_price - atr * 3.0, 2)
        else:
            # LONG: entry near current, stop below, profit above
            entry_zone = [round(cur_price - atr * 0.5, 2), round(cur_price + atr * 0.3, 2)]
            stop_loss = round(cur_price - atr * 1.8, 2)
            take_profit = round(cur_price + atr * 3.0, 2)
    else:
        entry_zone = [0, 0]
        stop_loss = 0
        take_profit = 0
    risk_reward = round(abs(take_profit - cur_price) / max(abs(stop_loss - cur_price), 0.01), 1)

    # ── Step 7: 审计标记 ──
    audit_flags = []
    if factor_validation.get("volume_breakout") == "FALSE_POSITIVE":
        audit_flags.append("缩量突破,警惕假突破")
    if non_vol_fp >= 1:
        audit_flags.append(f"{non_vol_fp}个非量因子FALSE_POSITIVE")
    if regime in ("RISK_OFF", "HIGH_VOL"):
        if direction_hint == "LONG":
            audit_flags.append(f"Regime={regime},LONG方向受限")
        else:
            audit_flags.append(f"Regime={regime},SHORT方向占优")
    if hist and hist.get("win_rate", 50) < 30:
        audit_flags.append(f"历史胜率仅{hist['win_rate']:.0f}%")

    return {
        "symbol": sym,
        "verdict": verdict,
        "direction": direction_hint,
        "confidence": confidence,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward": risk_reward,
        "position_size_pct": min(8, max(3, int(composite * 8))),
        "invalidation": f"{'收盘破' if direction_hint == 'LONG' else '收盘上破'} {stop_loss:.2f} (1.8x ATR)",
        "trigger_condition": f"价格进入 {entry_zone[0]:.2f}-{entry_zone[1]:.2f} 区间",
        "execution_rules": {
            "gap_up_over_2pct": "SKIP",
            "gap_down_over_2pct": "SKIP",
            "hit_zone_then_drop": "WAIT_5MIN_CANDLE_ABOVE",
        },
        "factor_validation": factor_validation,
        "audit_flags": audit_flags,
        "factor_score": composite,
        "model_used": "deterministic_rules",
        "_needs_llm": (verdict == "CONDITIONAL"),
    }


def _validate_single(candidate: Dict, context: Dict) -> Dict:
    """验证单个候选 — 确定性规则引擎优先, CONDITIONAL 走 LLM 深度判断。

    P0 (2026-06-17): 从纯 LLM 改为 代码优先 + LLM 补充。
    - VALID/INVALID: 确定性规则直接判定 (0 LLM, <1ms)
    - CONDITIONAL: 规则判定后追加 LLM 深度分析 (1 LLM, ~5-15s)
    """
    # Step 1: 确定性规则引擎 (必走)
    result = _validate_single_deterministic(candidate, context)

    # Step 2: CONDITIONAL → LLM 深度判断 (边界案例需要更细致的分析)
    if result.get("_needs_llm"):
        try:
            from app.llm.unified_service import UnifiedLLMService
            prompt = _build_candidate_prompt(candidate, context)
            # 附加确定性分析的初步结论, 让 LLM 做 confirm/override
            prompt += f"\n\n## 确定性规则引擎初步判定\n"
            prompt += f"- verdict: CONDITIONAL (边界案例)\n"
            prompt += f"- confidence: {result['confidence']}\n"
            prompt += f"- factor_validation: {json.dumps(result['factor_validation'], ensure_ascii=False)}\n"
            prompt += f"- audit_flags: {result['audit_flags']}\n"
            prompt += f"\n请基于以上信息, 给出你的最终判定。可以升级为 VALID 或降级为 INVALID。"

            llm_result = UnifiedLLMService.call(
                "plan_validator",
                prompt,
                system=SYSTEM_PROMPT,
                temperature=0,
                max_tokens=500,
                json_mode=True,
                timeout=60,  # 从 300s 降到 60s (仅处理边界案例)
            )

            if llm_result.success:
                raw = llm_result.response
                if isinstance(raw, str):
                    raw = raw.strip()
                    if raw.startswith("```"):
                        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = None
                elif isinstance(raw, dict):
                    parsed = raw
                else:
                    parsed = None

                if parsed and isinstance(parsed, dict):
                    # LLM 可以升级或降级 CONDITIONAL
                    llm_verdict = parsed.get("verdict", "")
                    if llm_verdict in ("VALID", "INVALID"):
                        result["verdict"] = llm_verdict
                        result["confidence"] = max(0, min(100, int(parsed.get("confidence", result["confidence"]))))
                        # 采用 LLM 的入场参数 (通常更精确, 引用了具体支撑阻力)
                        if parsed.get("entry_zone") and len(parsed["entry_zone"]) >= 2:
                            result["entry_zone"] = parsed["entry_zone"]
                        if parsed.get("stop_loss"):
                            result["stop_loss"] = parsed["stop_loss"]
                        if parsed.get("take_profit"):
                            result["take_profit"] = parsed["take_profit"]
                        if parsed.get("risk_reward"):
                            result["risk_reward"] = parsed["risk_reward"]
                        if parsed.get("factor_validation"):
                            result["factor_validation"] = parsed["factor_validation"]
                        if parsed.get("audit_flags"):
                            result["audit_flags"] = parsed["audit_flags"]
                        if parsed.get("execution_rules"):
                            result["execution_rules"] = parsed["execution_rules"]
                        if parsed.get("invalidation"):
                            result["invalidation"] = parsed["invalidation"]
                    result["model_used"] = llm_result.model_used or "deterministic+llm"

        except Exception as e:
            logger.warning(f"[Validator] {candidate['symbol']} LLM fallback failed: {e}, keeping deterministic result")
            # LLM 失败 → 保留确定性结果 (CONDITIONAL), 不阻断

    # 清理内部标记
    result.pop("_needs_llm", None)
    return result


def _get_peer_changes(candidates: List[Dict]) -> Dict[str, Dict]:
    """获取候选股的板块peers涨跌"""
    from app.plan.factor_engine import _SECTOR_MEMBERS
    result = {}

    all_peers = set()
    for c in candidates:
        short = c["symbol"].replace("US.", "")
        for sec in c.get("sectors", []):
            for ticker in _SECTOR_MEMBERS.get(sec, []):
                if ticker != short:
                    all_peers.add(f"US.{ticker}")

    if not all_peers:
        return result

    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        svc = ManualOrderService()
        if not svc.connect():
            return result

        from futu import RET_OK
        peer_list = list(all_peers)[:15]
        svc._quote_ctx.subscribe(peer_list, [])
        time.sleep(0.3)
        ret, snap = svc._quote_ctx.get_market_snapshot(peer_list)
        if ret == RET_OK and not snap.empty:
            for _, row in snap.iterrows():
                code = row.get("code", "").replace("US.", "")
                prev = float(row.get("prev_close_price", 0) or 0)
                cur = float(row.get("cur_price", 0) or 0)
                if prev > 0 and cur > 0:
                    # Map to candidate symbols
                    for c in candidates:
                        short = c["symbol"].replace("US.", "")
                        for sec in c.get("sectors", []):
                            if code in _SECTOR_MEMBERS.get(sec, []) and code != short:
                                result.setdefault(c["symbol"], {})[code] = round((cur - prev) / prev * 100, 2)

        svc.disconnect()
    except Exception as e:
        logger.debug(f"[Validator] peer changes error: {e}")

    return result


def _get_trade_history(symbols: List[str]) -> Dict[str, Dict]:
    """获取候选股的历史交易表现"""
    result = {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        for sym in symbols:
            rows = conn.execute(
                "SELECT direction, realized_pnl, entry_time, exit_time "
                "FROM trade_records WHERE symbol=? AND trade_mode != 'simulate' "
                "ORDER BY exit_time DESC LIMIT 5",
                (sym,)
            ).fetchall()
            if rows:
                wins = sum(1 for r in rows if float(r["realized_pnl"] or 0) > 0)
                total_pnl = sum(float(r["realized_pnl"] or 0) for r in rows)
                result[sym] = {
                    "count": len(rows),
                    "wins": wins,
                    "win_rate": round(wins / len(rows) * 100, 0),
                    "total_pnl": round(total_pnl, 2),
                    "last_trades": [
                        {"direction": r["direction"], "pnl": round(float(r["realized_pnl"] or 0), 2),
                         "date": (r["exit_time"] or "")[:10]}
                        for r in rows
                    ],
                }
        conn.close()
    except Exception:
        pass
    return result


def _get_bear_cases() -> Dict[str, str]:
    """从当前battle plan获取bear cases"""
    plan_path = Path(__file__).parent.parent.parent / "data" / "daily_battle_plan_us.json"
    if not plan_path.exists():
        return {}
    try:
        plan = json.loads(plan_path.read_text())
        result = {}
        for bc in plan.get("bear_cases", []):
            sym = bc.get("symbol", "")
            if sym:
                result[sym] = bc.get("bear_case", "") or bc.get("rationale", "")
        return result
    except Exception:
        return {}


def validate_candidates(factor_screen: Dict, max_workers: int = 4) -> Dict:
    """
    对factor_screen的Top N候选执行LLM验证。
    
    Args:
        factor_screen: factor_engine.run_factor_screen() 的输出
        max_workers: 并行LLM调用数
    
    Returns: {
        "timestamp": "...",
        "regime": "...",
        "validated": [
            {"symbol": "US.NVDA", "verdict": "VALID", "confidence": 72, ...},
            ...
        ],
        "valid_count": 5,
        "invalid_count": 3,
        "elapsed_sec": 32.5
    }
    """
    t0 = time.time()
    candidates = factor_screen.get("candidates", [])
    if not candidates:
        return {"validated": [], "valid_count": 0, "invalid_count": 0, "elapsed_sec": 0}

    # Build context
    context = {
        "regime": factor_screen.get("regime", "NEUTRAL"),
        "spy_change_20d": factor_screen.get("spy_change_20d", 0),
        "sector_momentum": factor_screen.get("sector_momentum", {}),
        "peer_changes": _get_peer_changes(candidates),
        "trade_history": _get_trade_history([c["symbol"] for c in candidates]),
        "bear_cases": _get_bear_cases(),
    }

    # Parallel LLM validation
    validated = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_validate_single, c, context): c["symbol"]
            for c in candidates
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                result = future.result()
                validated.append(result)
            except Exception as e:
                logger.error(f"[Validator] {sym} future error: {e}")
                validated.append({"symbol": sym, "verdict": "INVALID", "confidence": 0, "error": str(e)})

    # Sort by composite_score (factor_score) descending
    validated.sort(key=lambda v: v.get("factor_score", 0), reverse=True)

    valid_count = sum(1 for v in validated if v.get("verdict") == "VALID")
    conditional_count = sum(1 for v in validated if v.get("verdict") == "CONDITIONAL")
    invalid_count = sum(1 for v in validated if v.get("verdict") == "INVALID")

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": factor_screen.get("regime", "NEUTRAL"),
        "validated": validated,
        "valid_count": valid_count,
        "conditional_count": conditional_count,
        "invalid_count": invalid_count,
        "elapsed_sec": round(time.time() - t0, 1),
    }

    # Save
    try:
        out_path = Path(__file__).parent.parent.parent / "data" / "llm_validation_latest.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    except Exception:
        pass

    logger.info(
        f"[Validator] ✅ {len(validated)}只验证完成 | "
        f"VALID={valid_count} CONDITIONAL={conditional_count} INVALID={invalid_count} | "
        f"{result['elapsed_sec']}s"
    )
    return result


if __name__ == "__main__":
    """CLI: python -m app.plan.llm_validator"""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

    # Load factor screen
    fs_path = Path(__file__).parent.parent.parent / "data" / "factor_screen_latest.json"
    if not fs_path.exists():
        print("No factor_screen_latest.json found. Run factor_engine first.")
        sys.exit(1)

    factor_screen = json.loads(fs_path.read_text())
    result = validate_candidates(factor_screen)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
