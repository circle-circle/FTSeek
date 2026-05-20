"""
新闻 LLM 分类器 (L1 + L12 多语言, 2026-05-14)

输入: UnifiedNewsItemV2
输出 (JSON 强制 schema):
{
  "event_type": "earnings|fda_approval|merger|guidance_cut|geopolitical|fed_speech|rumor|other",
  "primary_tickers": ["NVDA"],
  "impacted_tickers": [{"ticker":"AMD","direction":"DOWN","confidence":0.6}],
  "sentiment": -0.7,                 # [-1, 1]
  "time_horizon_minutes": 60,        # 影响窗口
  "credibility": 0.8,                # [0, 1]
  "action_hint": "WAIT|BUY|SELL|HEDGE",
  "rationale": "短句 < 100 字"
}

可控:
- 输出强制 JSON schema, 不通过 → drop
- credibility < 0.5 → 不生成信号 (仅记录)
- 同 ticker 5min cooldown
- 默认 disabled
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import parse_and_validate, clamp_numeric
# L1 修复 (2026-05-19): 迁 model_call (yaml.llm.features.news_classifier=qwen-flash 路由).
# 旧 llm_helper 仍可用但不走新 model 路由, 5/19 16 笔实测 0% 成功率 (走 minimax 老路径).
from app.llm.model_call import call_llm as _model_call_llm

logger = logging.getLogger(__name__)

ALLOWED_EVENT_TYPES = [
    "earnings", "guidance_cut", "guidance_raise",
    "fda_approval", "fda_rejection",
    "merger", "acquisition", "spinoff",
    "geopolitical", "fed_speech", "rate_decision",
    "rumor", "analyst_upgrade", "analyst_downgrade",
    "product_launch", "lawsuit", "executive_change",
    "other",
]
ALLOWED_ACTION = ["WAIT", "BUY", "SELL", "HEDGE", "OBSERVE"]
ALLOWED_DIRECTION = ["UP", "DOWN", "NEUTRAL"]


@dataclass
class NewsClassification:
    news_id: str
    event_type: str
    primary_tickers: List[str]
    impacted_tickers: List[Dict[str, Any]]   # [{ticker, direction, confidence}]
    sentiment: float
    time_horizon_minutes: int
    credibility: float
    action_hint: str
    rationale: str
    classified_at: float = field(default_factory=time.time)
    raw_lang: str = "en"


def _build_system_prompt(lang: str = "en") -> str:
    if lang == "zh":
        return (
            "你是 FTSeek 量化系统的新闻分析师. 给定一条新闻, 提取结构化信息. "
            "严格按 JSON schema 输出, 不要任何额外文本."
        )
    if lang == "ja":
        return (
            "あなたは FTSeek 量子システムのニュース分析者です. ニュースから構造化情報を抽出してください. "
            "JSON スキーマに厳密に従って出力し, 余分なテキストを含めないでください."
        )
    return (
        "You are FTSeek's news analyst for US markets. Extract structured info from one news item. "
        "Respond with strict JSON only, no extra text."
    )


def _build_user_prompt(title: str, content: str, source: str, ts: float) -> str:
    age_min = (time.time() - ts) / 60
    body = (content or "")[:1500]  # 截断防 prompt 爆炸
    return f"""News:
Source: {source}
Age: {age_min:.0f} minutes ago
Title: {title}
Body: {body}

Extract and return JSON only:
{{
  "event_type": "earnings|guidance_cut|guidance_raise|fda_approval|fda_rejection|merger|acquisition|spinoff|geopolitical|fed_speech|rate_decision|rumor|analyst_upgrade|analyst_downgrade|product_launch|lawsuit|executive_change|other",
  "primary_tickers": ["TICKER1", "TICKER2"],
  "impacted_tickers": [{{"ticker":"TICKER","direction":"UP|DOWN|NEUTRAL","confidence":0.0-1.0}}],
  "sentiment": -1.0 to 1.0,
  "time_horizon_minutes": 5-1440,
  "credibility": 0.0-1.0,
  "action_hint": "WAIT|BUY|SELL|HEDGE|OBSERVE",
  "rationale": "<= 100 chars"
}}
Rules:
- primary_tickers: at most 3
- impacted_tickers: at most 5
- credibility < 0.5 means rumor/uncorroborated source
- horizon should reflect realistic price impact window
- action_hint OBSERVE for ambiguous, WAIT for waiting confirmation"""


def _detect_lang(title: str, content: str) -> str:
    """简单语言识别 (无依赖)

    优先级: 日文假名 (hiragana/katakana) > 中文 > 英文.
    日文文本通常含 kanji+假名混合, 仅 kanji 不能区分中日文.
    """
    text = (title + " " + (content or "")[:500])
    if not text:
        return "en"
    # 日文假名: ぁ-ヿ
    jp_kana = sum(1 for c in text if "぀" <= c <= "ヿ")
    # 中文 + 日文 kanji: 一-龯 (CJK Unified Ideographs)
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    total = max(1, len(text))
    # 优先看假名: 有假名即日文
    if jp_kana / total > 0.03:
        return "ja"
    if cjk / total > 0.10:
        return "zh"
    return "en"


def classify_news(
    news_id: str,
    title: str,
    content: str,
    source: str = "",
    timestamp: float = None,
) -> Optional[NewsClassification]:
    """对单条新闻分类. 默认 disabled. 返回 None 表示未分类."""
    if not is_enabled("news_classifier"):
        return None

    if timestamp is None:
        timestamp = time.time()

    cfg = get_feature_config("news_classifier")
    multilang = is_enabled("news_multilang")

    lang = _detect_lang(title, content) if multilang else "en"
    system = _build_system_prompt(lang)
    prompt = _build_user_prompt(title, content, source, timestamp)

    # L1 修复 (2026-05-19): 走新 model_call (yaml.llm.features.news_classifier 路由).
    # schema 校验内置, 失败时 audit 自动保留 raw (L2-B), 数据驱动诊断.
    res = _model_call_llm(
        feature="news_classifier",
        prompt=prompt,
        system=system,
        temperature=0.0,
        max_tokens=500,
        timeout=int(cfg.get("timeout_sec", 20)),
        schema_required={
            "event_type": str,
            "primary_tickers": list,
            "impacted_tickers": list,
            "sentiment": float,
            "time_horizon_minutes": int,
            "credibility": float,
            "action_hint": str,
            "rationale": str,
        },
        schema_enums={
            "event_type": ALLOWED_EVENT_TYPES,
            "action_hint": ALLOWED_ACTION,
        },
    )
    if not res or not res.success:
        return None
    obj = res.response if isinstance(res.response, dict) else None
    if obj is None:
        return None

    # 数值钳位
    obj["sentiment"] = clamp_numeric(obj["sentiment"], -1.0, 1.0)
    obj["credibility"] = clamp_numeric(obj["credibility"], 0.0, 1.0)
    obj["time_horizon_minutes"] = int(clamp_numeric(obj["time_horizon_minutes"], 5, 1440))
    # 处理 impacted_tickers 内部
    cleaned_impacted = []
    for it in obj.get("impacted_tickers", []):
        if not isinstance(it, dict):
            continue
        direction = str(it.get("direction", "NEUTRAL"))
        if direction not in ALLOWED_DIRECTION:
            direction = "NEUTRAL"
        cleaned_impacted.append({
            "ticker": str(it.get("ticker", "")).upper(),
            "direction": direction,
            "confidence": clamp_numeric(it.get("confidence", 0.5), 0.0, 1.0),
        })
    cleaned_impacted = cleaned_impacted[:5]

    classification = NewsClassification(
        news_id=news_id,
        event_type=obj["event_type"],
        primary_tickers=[str(t).upper() for t in obj["primary_tickers"][:3]],
        impacted_tickers=cleaned_impacted,
        sentiment=obj["sentiment"],
        time_horizon_minutes=obj["time_horizon_minutes"],
        credibility=obj["credibility"],
        action_hint=obj["action_hint"],
        rationale=obj["rationale"][:200],
        raw_lang=lang,
    )

    # 落库
    _persist(classification)

    logger.info(
        f"[NewsClassifier] {news_id[:12]} {classification.event_type} "
        f"primary={classification.primary_tickers} sentiment={classification.sentiment:+.2f} "
        f"credibility={classification.credibility:.2f} action={classification.action_hint}"
    )
    return classification


def _persist(c: NewsClassification):
    """写入 news_quality.db"""
    try:
        import sqlite3
        from app.core.paths import get_data_dir
        import json as _json
        db = str(get_data_dir() / "news_quality.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_classifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id TEXT NOT NULL,
                event_type TEXT,
                primary_tickers TEXT,
                impacted_tickers_json TEXT,
                sentiment REAL,
                horizon_min INTEGER,
                credibility REAL,
                action_hint TEXT,
                rationale TEXT,
                raw_lang TEXT,
                classified_at REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nc_news_id ON news_classifications(news_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nc_event ON news_classifications(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nc_time ON news_classifications(classified_at)")
        conn.execute(
            "INSERT INTO news_classifications "
            "(news_id, event_type, primary_tickers, impacted_tickers_json, sentiment, "
            " horizon_min, credibility, action_hint, rationale, raw_lang, classified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.news_id, c.event_type, ",".join(c.primary_tickers),
                _json.dumps(c.impacted_tickers, ensure_ascii=False),
                c.sentiment, c.time_horizon_minutes, c.credibility,
                c.action_hint, c.rationale, c.raw_lang, c.classified_at,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[NewsClassifier·persist] 失败: {e}")
