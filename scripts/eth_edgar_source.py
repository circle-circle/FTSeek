#!/usr/bin/env python3
"""SEC EDGAR 8-K Item 2.02 观测源 (RFC-104 阶段 0 增强批, 2026-09-24).

## 实证结论 (2026-09-24 AAPL 冒烟, CIK=0000320193)
- ``data.sec.gov/submissions/CIK{cik}.json`` 的 ``filings.recent`` **包含 items 字段** ✅
- items 字段是逗号分隔字符串, 如 ``"2.02,9.01"`` (Item 2.02 = Results of Operations
  and Financial Condition = 业绩发布; 9.01 = Financial Statements and Exhibits)
- ``acceptanceDateTime`` 给出精确 UTC 时间戳 (如 ``"2026-07-30T20:30:28.000Z"``),
  可转换为 ET 时刻判 BMO/AMC —— **EDGAR 精确到秒级, 非仅日期级**
- ``filingDate`` 是日期级 (YYYY-MM-DD); acceptanceDateTime 是提交被接受的时刻
- recent filings 上限 1000 条; 更多需 ``filings.files`` 分页 (本模块不需要, 8 次历史足够)

## 合规纪律 (SEC EDGAR Fair Access Rules)
- User-Agent **必须**声明身份: 格式 ``"Name email@example.com"`` (可从 env ``EDGAR_UA`` 覆写)
- 请求频率 ≤ 10 req/s (实际每天 7-9 票远低于此上限)
- 全部请求带 timeout (15s); 失败返回 None/[] + logger.warning(exc_info=True)
- 缓存优先: ticker→CIK 映射日级 TTL; 同 CIK 当日不重拉 submissions
- 本模块**只读** SEC 公开数据, 不修改任何台账/计划

## 优雅降级 (生产 cron 纪律)
- 任何 EDGAR 失败只 warning 留痕并返回 None/[], 绝不让生成/刷新/对账崩溃
- env ``ETH_PLAN_EDGAR_ENABLED=false`` 可一键断源回退 (所有公开函数立即返回 None)
- HTTP 层隔离在 ``_fetch`` 函数里, 便于 mock 测试 (零真连接)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"

# ── 缓存 TTL ──
_TICKER_CIK_TTL_SECONDS = 86400  # ticker→CIK 映射: 日级
_SUBMISSIONS_TTL_SECONDS = 86400  # submissions: 日级 (同 CIK 当日不重拉)

# ── HTTP 配置 ──
_EDGAR_BASE = "https://data.sec.gov"
_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
_TIMEOUT = 15  # 秒
_MAX_RECENT_8K = 8  # 习惯先验取近 8 次 2.02 申报


def _ua() -> str:
    """User-Agent 声明身份 (SEC EDGAR Fair Access Rules)."""
    return os.getenv("EDGAR_UA", "FTSeek-Research admin@ftseek.com")


def _edgar_enabled() -> bool:
    """env 开关: ETH_PLAN_EDGAR_ENABLED (默认 true)."""
    return os.getenv("ETH_PLAN_EDGAR_ENABLED", "true").lower() != "false"


# ── HTTP 层 (隔离便于 mock) ──

def _fetch(url: str) -> bytes | None:
    """HTTP GET with UA + timeout. 失败返回 None + warning."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        return resp.read()
    except Exception as exc:
        logger.warning("EDGAR fetch failed %s: %s: %s", url, type(exc).__name__, exc,
                       exc_info=True)
        return None


# ── 缓存层 ──

def _cache_path(name: str) -> Path:
    """缓存文件路径: data/cache/<name>."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _cache_read(path: Path, ttl_seconds: int) -> dict | list | None:
    """读缓存: 文件存在且未过期则返回 parsed JSON, 否则 None."""
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        if time.time() - mtime > ttl_seconds:
            return None  # 过期
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cache read failed %s: %s", path, exc, exc_info=True)
        return None


def _cache_write(path: Path, data: Any) -> None:
    """写缓存: 原子写 JSON."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("cache write failed %s: %s", path, exc, exc_info=True)


# ── ticker → CIK ──

def _load_ticker_map() -> dict[str, int] | None:
    """加载 ticker→CIK 全量映射 (缓存优先, 日级 TTL).

    返回 {"AAPL": 320193, ...} 或 None (失败).
    """
    cache = _cache_path("edgar_company_tickers.json")
    cached = _cache_read(cache, _TICKER_CIK_TTL_SECONDS)
    if cached is not None:
        return cached

    raw = _fetch(_TICKER_URL)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("ticker map JSON parse failed: %s", exc, exc_info=True)
        return None

    # SEC format: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, "1": {...}, ...}
    ticker_map: dict[str, int] = {}
    for _key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).upper()
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            ticker_map[ticker] = int(cik)

    _cache_write(cache, ticker_map)
    return ticker_map


def ticker_to_cik(symbol: str) -> int | None:
    """ticker → CIK. 失败返回 None."""
    if not _edgar_enabled():
        return None
    ticker = symbol.replace("US.", "").upper()
    tmap = _load_ticker_map()
    if tmap is None:
        return None
    return tmap.get(ticker)


# ── submissions ──

def _load_submissions(cik: int) -> dict | None:
    """加载 CIK 的 submissions JSON (缓存优先, 日级 TTL).

    返回 ``filings.recent`` 字典 (含 form/filingDate/items/acceptanceDateTime 等平行数组),
    或 None (失败).
    """
    cik_padded = str(cik).zfill(10)
    cache = _cache_path(f"edgar_submissions_{cik_padded}.json")
    cached = _cache_read(cache, _SUBMISSIONS_TTL_SECONDS)
    if cached is not None:
        return cached

    url = f"{_EDGAR_BASE}/submissions/CIK{cik_padded}.json"
    raw = _fetch(url)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("submissions JSON parse failed for CIK %s: %s", cik, exc,
                       exc_info=True)
        return None

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        logger.warning("submissions missing filings.recent for CIK %s", cik)
        return None

    _cache_write(cache, recent)
    return recent


def _find_8k_2020_filings(recent: dict, target_date: str | None = None) -> list[dict]:
    """从 submissions.recent 筛 8-K (非 8-K/A) 含 items 2.02 的申报.

    参数:
        recent: filings.recent 字典 (平行数组)
        target_date: 可选 YYYY-MM-DD, 只返回该日申报; None = 返回全部

    返回: [{"filingDate", "acceptanceDateTime", "accessionNumber", "items"}, ...]
    """
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items_list = recent.get("items", [])
    accept_times = recent.get("acceptanceDateTime", [])
    accessions = recent.get("accessionNumber", [])

    results = []
    for i in range(len(forms)):
        form = str(forms[i]) if i < len(forms) else ""
        # 严格: form == "8-K" (不含 8-K/A 修正件)
        if form != "8-K":
            continue
        items_str = str(items_list[i]) if i < len(items_list) else ""
        # items 含 "2.02" (逗号分隔的列表里匹配)
        item_tokens = [t.strip() for t in items_str.split(",")]
        if "2.02" not in item_tokens:
            continue

        filing_date = str(dates[i]) if i < len(dates) else ""
        if target_date and filing_date[:10] != target_date:
            continue

        results.append({
            "filingDate": filing_date[:10],
            "acceptanceDateTime": str(accept_times[i]) if i < len(accept_times) else "",
            "accessionNumber": str(accessions[i]) if i < len(accessions) else "",
            "items": items_str,
        })

    return results


def _acceptance_to_et_iso(acceptance_utc: str) -> str | None:
    """acceptanceDateTime (UTC ISO) → ET ISO 时刻. 解析失败返回 None."""
    if not acceptance_utc:
        return None
    try:
        # Format: "2026-07-30T20:30:28.000Z" or "2026-07-30T20:30:28Z"
        # Handle both with and without milliseconds
        cleaned = acceptance_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        dt_et = dt.astimezone(ET)
        return dt_et.isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def _acceptance_et_minutes(acceptance_utc: str) -> int | None:
    """acceptanceDateTime → ET 时刻的分钟数 (0-1439). 解析失败返回 None."""
    et_iso = _acceptance_to_et_iso(acceptance_utc)
    if et_iso is None:
        return None
    try:
        dt = datetime.fromisoformat(et_iso)
        return dt.hour * 60 + dt.minute
    except (ValueError, TypeError):
        return None


# ── 公开函数 (三个纯函数, HTTP 层隔离) ──

def edgar_8k_arrival(symbol: str, et_date: str) -> dict | None:
    """该票该 ET 日是否有 8-K Item 2.02 申报.

    返回:
        {"filed_at": "<ET ISO>", "accession": "<accessionNumber>"} 或 None.
        filed_at 是 acceptanceDateTime 转 ET 的精确时刻.

    优雅降级: EDGAR 不可用 / 票无 CIK / 无申报 → 返回 None.
    """
    if not _edgar_enabled():
        return None

    cik = ticker_to_cik(symbol)
    if cik is None:
        return None

    recent = _load_submissions(cik)
    if recent is None:
        return None

    filings = _find_8k_2020_filings(recent, target_date=et_date)
    if not filings:
        return None

    # 取第一个 (通常一天最多一个 8-K 2.02)
    f = filings[0]
    et_iso = _acceptance_to_et_iso(f.get("acceptanceDateTime", ""))
    if et_iso is None:
        return None

    return {
        "filed_at": et_iso,
        "accession": f.get("accessionNumber", ""),
    }


def edgar_phase_evidence(symbol: str, et_date: str) -> str | None:
    """按 EDGAR 8-K 提交时刻 ET 判相位证据.

    返回:
        "bmo" — 提交时刻 < 09:30 ET (盘前)
        "amc" — 提交时刻 ≥ 16:00 ET (盘后)
        None  — 中间时段 / 无申报 / EDGAR 不可用 (不强判)

    为什么 09:30 和 16:00:
        BMO 财报通常在 06:00-09:00 ET 发布, 8-K 提交紧随其后;
        AMC 财报通常在 16:00-17:00 ET 发布; 中间时段 (09:30-15:59) 的 8-K
        可能是前一日晚间提交或特殊情况, 不强判.
    """
    if not _edgar_enabled():
        return None

    cik = ticker_to_cik(symbol)
    if cik is None:
        return None

    recent = _load_submissions(cik)
    if recent is None:
        return None

    filings = _find_8k_2020_filings(recent, target_date=et_date)
    if not filings:
        return None

    minutes = _acceptance_et_minutes(filings[0].get("acceptanceDateTime", ""))
    if minutes is None:
        return None

    # < 09:30 ET (570 min) = BMO 证据
    if minutes < 570:
        return "bmo"
    # ≥ 16:00 ET (960 min) = AMC 证据
    if minutes >= 960:
        return "amc"
    # 中间时段: 不强判
    return None


def edgar_phase_habit(symbol: str) -> dict | None:
    """近 8 次 8-K Item 2.02 申报的时刻分布 → 习惯先验.

    返回:
        {
            "phase": "bmo" | "amc",   # 多数派相位
            "n_history": int,          # 实际分析的申报数 (≤8)
            "consistency": float,      # 多数派占比 (0.0-1.0)
            "basis": "edgar_8k_history"
        } 或 None (历史不足 / EDGAR 不可用).

    历史不足判据: n_history < 3 → 返回 None (不够判习惯).
    全一致: 8/8 全 AMC → phase="amc", consistency=1.0.
    混合: 5 AMC + 3 BMO → phase="amc", consistency=0.625.
    """
    if not _edgar_enabled():
        return None

    cik = ticker_to_cik(symbol)
    if cik is None:
        return None

    recent = _load_submissions(cik)
    if recent is None:
        return None

    # 取所有 8-K 2.02 申报 (不限日期)
    all_filings = _find_8k_2020_filings(recent)
    if not all_filings:
        return None

    # 取近 _MAX_RECENT_8K 次 (按 filingDate 降序, submissions 已经是降序)
    recent_filings = all_filings[:_MAX_RECENT_8K]

    # 分类: bmo / amc / unknown
    bmo_count = 0
    amc_count = 0
    for f in recent_filings:
        minutes = _acceptance_et_minutes(f.get("acceptanceDateTime", ""))
        if minutes is None:
            continue
        if minutes < 570:
            bmo_count += 1
        elif minutes >= 960:
            amc_count += 1

    n_classified = bmo_count + amc_count
    if n_classified < 3:
        return None  # 历史不足

    if amc_count >= bmo_count:
        phase = "amc"
        majority = amc_count
    else:
        phase = "bmo"
        majority = bmo_count

    return {
        "phase": phase,
        "n_history": n_classified,
        "consistency": round(majority / n_classified, 4),
        "basis": "edgar_8k_history",
    }
