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


def _validate_single(candidate: Dict, context: Dict) -> Dict:
    """验证单个候选 (在线程池中执行)"""
    sym = candidate["symbol"]
    try:
        from app.llm.unified_service import UnifiedLLMService
        prompt = _build_candidate_prompt(candidate, context)

        result = UnifiedLLMService.call(
            "plan_validator",
            prompt,
            system=SYSTEM_PROMPT,
            temperature=0,
            max_tokens=500,
            json_mode=True,
            timeout=300,
        )

        if result.success:
            raw = result.response
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
                    parsed = {"verdict": "INVALID", "confidence": 0,
                              "factor_validation": {}, "execution_rules": {},
                              "audit_flags": ["LLM输出解析失败"]}
            else:
                parsed = raw if isinstance(raw, dict) else {"verdict": "INVALID", "confidence": 0}

            # Validate & normalize
            if parsed.get("verdict") not in ("VALID", "INVALID", "CONDITIONAL"):
                parsed["verdict"] = "INVALID"
            parsed["confidence"] = max(0, min(100, int(parsed.get("confidence", 0))))
            parsed["symbol"] = sym
            parsed["model_used"] = result.model_used or ""
            parsed["factor_score"] = candidate["composite_score"]
            return parsed
        else:
            return {"symbol": sym, "verdict": "INVALID", "confidence": 0,
                    "error": result.error, "factor_score": candidate["composite_score"]}

    except Exception as e:
        logger.error(f"[Validator] {sym} error: {e}")
        return {"symbol": sym, "verdict": "INVALID", "confidence": 0,
                "error": str(e), "factor_score": candidate["composite_score"]}


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
