"""
Thesis Challenge — LLM审视用户的进场理由，给出绝对判断。

设计原则:
- json_mode=True, temperature=0, max_tokens=300 — 不允许废话
- 输出只有4个字段: verdict/weakness/fatal_if/confidence
- 每symbol 4h缓存，不重复调用
- verdict只有VALID/INVALID，没有"还行"
"""

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("ftseek_us.thesis_challenge")

CACHE_TTL_SECONDS = 4 * 3600  # 4 hours

_DB_PATH = None


def _get_db() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        try:
            from app.core.paths import get_trade_records_db
            _DB_PATH = str(get_trade_records_db())
        except Exception:
            _DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "ftseek_us_live.db")
    return _DB_PATH


def _ensure_table():
    """确保thesis_challenges表存在"""
    conn = sqlite3.connect(_get_db())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            thesis TEXT NOT NULL,
            verdict TEXT NOT NULL,
            weakness TEXT DEFAULT '',
            fatal_if TEXT DEFAULT '',
            confidence INTEGER DEFAULT 0,
            context_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            model_used TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_symbol ON thesis_challenges(symbol, created_at)")
    conn.commit()
    conn.close()


def get_cached_challenge(symbol: str) -> Optional[Dict[str, Any]]:
    """查询缓存，4h内有效则返回"""
    _ensure_table()
    conn = sqlite3.connect(_get_db())
    conn.row_factory = sqlite3.Row
    cutoff = datetime.fromtimestamp(time.time() - CACHE_TTL_SECONDS).isoformat()
    row = conn.execute(
        "SELECT * FROM thesis_challenges WHERE symbol=? AND created_at > ? ORDER BY id DESC LIMIT 1",
        (symbol, cutoff)
    ).fetchone()
    conn.close()
    if row:
        return {
            "symbol": row["symbol"],
            "thesis": row["thesis"],
            "verdict": row["verdict"],
            "weakness": row["weakness"],
            "fatal_if": row["fatal_if"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "model_used": row["model_used"],
            "cached": True,
        }
    return None


def _save_challenge(symbol: str, thesis: str, result: Dict, model_used: str = ""):
    """保存挑战结果到DB"""
    _ensure_table()
    conn = sqlite3.connect(_get_db())
    conn.execute(
        "INSERT INTO thesis_challenges (symbol, thesis, verdict, weakness, fatal_if, confidence, created_at, model_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            symbol,
            thesis,
            result.get("verdict", "UNKNOWN"),
            result.get("weakness", ""),
            result.get("fatal_if", ""),
            int(result.get("confidence", 0)),
            datetime.now().isoformat(),
            model_used,
        )
    )
    conn.commit()
    conn.close()


SYSTEM_PROMPT = """你是一个严苛的交易审计师。用户给了你他的进场理由(thesis)，你必须找出致命漏洞。

规则:
1. verdict必须是VALID或INVALID，不允许"还行"、"有待观察"
2. weakness必须是一句话，指出最致命的逻辑漏洞或事实矛盾
3. fatal_if必须是具体条件（价格/事件/时间），不是模糊的"市场恶化"
4. confidence是你对verdict判断的确信度(0-100)
5. 不要解释你的推理过程，不要给建议，只输出JSON
6. 如果thesis是"盘感"、"随机"、"跟风"等无实质内容的理由，直接INVALID

输出格式(严格JSON):
{
  "verdict": "VALID" 或 "INVALID",
  "weakness": "一句话最致命的漏洞",
  "fatal_if": "具体条件，触发则thesis必死",
  "confidence": 0-100整数
}"""


def challenge_thesis(
    symbol: str,
    thesis: str,
    direction: str = "",
    cur_price: float = 0,
    cost_price: float = 0,
    pnl_pct: float = 0,
    hold_days: int = 0,
    bear_cases: str = "",
) -> Dict[str, Any]:
    """
    调用LLM挑战thesis。先查缓存，无缓存则调用。
    
    Returns: {verdict, weakness, fatal_if, confidence, cached, model_used, created_at}
    """
    # 1. Check cache
    cached = get_cached_challenge(symbol)
    if cached:
        return cached

    # 2. Build prompt
    user_prompt = f"""标的: {symbol}
方向: {direction}
进场理由(thesis): {thesis}
当前价格: ${cur_price:.2f}
成本价格: ${cost_price:.2f}
当前盈亏: {pnl_pct:.1f}%
持仓天数: {hold_days}"""

    if bear_cases:
        user_prompt += f"\n\n空头审计师的质疑(来自战前计划): {bear_cases}"

    # 3. Call LLM
    try:
        from app.llm.unified_service import UnifiedLLMService
        result = UnifiedLLMService.call(
            "thesis_challenge",
            user_prompt,
            system=SYSTEM_PROMPT,
            temperature=0,
            max_tokens=300,
            json_mode=True,
            timeout=30,
        )

        if result.success:
            # Parse JSON from response
            raw = result.response
            if isinstance(raw, str):
                # Strip code block markers
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"verdict": "INVALID", "weakness": "LLM输出解析失败", "fatal_if": "需人工判断", "confidence": 0}
            else:
                parsed = raw if isinstance(raw, dict) else {"verdict": "INVALID", "weakness": "LLM返回异常", "fatal_if": "需人工判断", "confidence": 0}

            # Validate required fields
            if parsed.get("verdict") not in ("VALID", "INVALID"):
                parsed["verdict"] = "INVALID"
            parsed["confidence"] = max(0, min(100, int(parsed.get("confidence", 0))))
            parsed["weakness"] = str(parsed.get("weakness", ""))[:200]
            parsed["fatal_if"] = str(parsed.get("fatal_if", ""))[:200]

            # Save to DB
            _save_challenge(symbol, thesis, parsed, result.model_used or "")

            return {
                "symbol": symbol,
                "thesis": thesis,
                "verdict": parsed["verdict"],
                "weakness": parsed["weakness"],
                "fatal_if": parsed["fatal_if"],
                "confidence": parsed["confidence"],
                "created_at": datetime.now().isoformat(),
                "model_used": result.model_used or "",
                "cached": False,
            }
        else:
            logger.error(f"[ThesisChallenge] LLM调用失败: {result.error}")
            return {"error": result.error or "LLM调用失败", "cached": False}

    except Exception as e:
        logger.error(f"[ThesisChallenge] 异常: {e}")
        return {"error": str(e), "cached": False}
