#!/usr/bin/env python3
"""SEC EDGAR 8-K 直连 provider — S3 (2026-07-27, DEB-20260727 §三/§六)。

背景: RSS 转述链发布→入库 p50=74min; EDGAR getcurrent Atom 是分钟级免费官方一手源,
8-K = 重大事件 (天然 HARD_EVENT), CIK→ticker 精确映射 (标的零猜测)。
本模块只读拉取+缓存, 消费端 = llm_universe_scan._build_clue_brief [硬事件] 段
(config: universe_expansion.clue_8k_events)。

方向先验 (DEB §六.3 增补): 8-K item code 是渠道侧唯一直接带方向语义的信息 —
  2.02 业绩公布 / 1.01 重大协议 / 7.01|8.01 自愿披露 / 4.02 财务重述(利空) /
  1.03 破产(利空) / 5.02 高管变动 / 2.01 并购完成。item 字段完整落 brief 供 LLM 消费,
  预注册: 攒样本后按 item×fwd 切片判方向先验有效性 (S1 同框架)。

噪声控制 (DEB §五.1): item 白名单 + 标的池过滤 (指令台账/topup/watchlist 出现过的 ticker)。
用法 (独立冒烟): PYTHONPATH=. ./venv/bin/python -m app.data.sec_8k_provider
缓存: data/sec_cache/edgar_8k_recent.json (10min TTL)。回滚: clue_8k_events 不配或 false。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import Request, urlopen

logger = logging.getLogger("ftseek_us.sec_8k")

from app.data.sec_form4_provider import SEC_USER_AGENT, CACHE_DIR  # 复用框架常量

GETCURRENT_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                  "&type=8-K&company=&dateb=&owner=include&count=100&output=atom")
# C2 (2026-07-27, DEB-20260727 §九/§十): SC 13D/G 大股东举牌 — 常先于新闻,
#   取 (Subject) 方 = 被举牌标的 (Filed by 方是举牌人, 非交易标的)
GETCURRENT_13DG_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                       "&type=SC+13&company=&dateb=&owner=include&count=100&output=atom")
CACHE_FILE = CACHE_DIR / "edgar_8k_recent.json"
CACHE_13DG_FILE = CACHE_DIR / "edgar_13dg_recent.json"
CACHE_TTL_SEC = 600  # 10min — scan 是盘前批调用, 无需更细

# item code → (中文语义, 方向先验)  先验只是提示词标注, 不进任何执行逻辑
ITEM_SEMANTICS: Dict[str, tuple] = {
    "1.01": ("重大协议签订", "看多倾向"),
    "1.02": ("重大协议终止", "看空倾向"),
    "1.03": ("破产/接管", "强利空"),
    "2.01": ("资产收购/出售完成", "事件驱动"),
    "2.02": ("业绩公布", "方向取决于超预期与否"),
    "2.05": ("重组/裁员成本", "短期利空长期存疑"),
    "2.06": ("重大减值", "利空"),
    "4.02": ("财报不可依赖(重述)", "强利空"),
    "5.02": ("高管/董事变动", "CEO离任常利空"),
    "7.01": ("自愿披露(Reg FD)", "事件驱动"),
    "8.01": ("其他重大事件", "事件驱动"),
}
ITEM_WHITELIST = set(ITEM_SEMANTICS)


def _fetch_atom(url: str = GETCURRENT_URL) -> str:
    req = Request(url, headers={"User-Agent": SEC_USER_AGENT})
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_entries(atom: str) -> List[Dict]:
    """轻量解析 getcurrent Atom (无第三方依赖): title 含 '8-K - 公司名 (CIK) (Filer)',
    summary 含 'Filed: ... AccNo: ...' 及 item 列表。"""
    out = []
    for m in re.finditer(r"<entry>(.*?)</entry>", atom, re.S):
        e = m.group(1)
        title = re.search(r"<title>(.*?)</title>", e, re.S)
        updated = re.search(r"<updated>(.*?)</updated>", e)
        summary = re.search(r"<summary[^>]*>(.*?)</summary>", e, re.S)
        link = re.search(r'href="([^"]+)"', e)
        t = (title.group(1) if title else "").strip()
        if not t.upper().startswith("8-K"):
            continue
        cik_m = re.search(r"\((\d{7,10})\)", t)
        comp_m = re.search(r"8-K[^-]*-\s*(.+?)\s*\(", t)
        s = (summary.group(1) if summary else "")
        items = sorted(set(re.findall(r"Item\s+(\d+\.\d+)", s)) & ITEM_WHITELIST)
        out.append({
            "company": (comp_m.group(1) if comp_m else t)[:80],
            "cik": int(cik_m.group(1)) if cik_m else None,
            "items": items,
            "updated": (updated.group(1) if updated else "")[:25],
            "url": link.group(1) if link else "",
        })
    return out


def fetch_recent_8k(universe_tickers: Optional[set] = None,
                    lookback_hours: int = 24) -> List[Dict]:
    """近 N 小时 8-K (item 白名单命中 + 标的池过滤 ticker 非空)。带磁盘缓存。

    返回 [{ticker, company, items, item_labels, updated, url}]
    """
    # 缓存
    raw_entries = None
    try:
        if CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL_SEC:
            raw_entries = json.loads(CACHE_FILE.read_text())
    except Exception:
        raw_entries = None
    if raw_entries is None:
        try:
            raw_entries = _parse_entries(_fetch_atom())
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(raw_entries, ensure_ascii=False))
        except Exception as e:
            logger.warning("[8K] EDGAR getcurrent 拉取失败 (降级空): %s", e)
            return []

    # CIK → ticker (复用 form4 的映射加载)
    try:
        from app.data.sec_form4_provider import SECForm4Provider
        prov = SECForm4Provider()
        cik2tk = prov._cik_ticker_map or {}
    except Exception as e:
        logger.warning("[8K] CIK 映射不可用 (降级空): %s", e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out = []
    for e in raw_entries:
        if not e.get("items"):
            continue  # item 白名单未命中 → 噪声, 丢
        tk = cik2tk.get(e.get("cik") or -1)
        if not tk:
            continue  # 无 ticker 映射 (私募债/信托等) → 丢
        tk = str(tk).upper()
        if universe_tickers and tk not in universe_tickers:
            continue
        try:
            upd = datetime.fromisoformat(e["updated"].replace("Z", "+00:00"))
            if upd < cutoff:
                continue
        except Exception:
            pass
        out.append({
            "ticker": tk, "company": e["company"], "items": e["items"],
            "item_labels": [f"{i} {ITEM_SEMANTICS[i][0]}({ITEM_SEMANTICS[i][1]})"
                            for i in e["items"]],
            "updated": e["updated"], "url": e["url"],
        })
    return out


def fetch_recent_13dg(universe_tickers: Optional[set] = None,
                      lookback_hours: int = 96) -> List[Dict]:
    """C2: 近 N 小时 SC 13D/G 举牌 (取 Subject 方=被举牌标的)。默认 96h 回看
    (举牌有 10 日披露窗, 信号衰减慢于新闻)。

    返回 [{ticker, company, form (SC 13D=主动举牌强信号 / SC 13G=被动持仓), updated, url}]
    格式假设: title = 'SC 13D/A - COMPANY (CIK) (Subject)' — 与 8-K 实测同源格式;
    解析不到 (Subject) 则丢弃 (宁缺勿错, 测量层准确性优先)。
    """
    raw = None
    try:
        if CACHE_13DG_FILE.exists() and time.time() - CACHE_13DG_FILE.stat().st_mtime < CACHE_TTL_SEC:
            raw = json.loads(CACHE_13DG_FILE.read_text())
    except Exception:
        raw = None
    if raw is None:
        try:
            atom = _fetch_atom(GETCURRENT_13DG_URL)
            raw = []
            for m in re.finditer(r"<entry>(.*?)</entry>", atom, re.S):
                e = m.group(1)
                title = re.search(r"<title>(.*?)</title>", e, re.S)
                t = (title.group(1) if title else "").strip()
                # 只要 Subject 方 (被举牌标的); Filed by 方=举牌人, 丢.
                # 2026-10-18: 必须只收 SC 13D/G — 原 startswith("SC 13") 会把
                #   SC 13E3 (私有化/going-private) 也收进来, 且 form 回落成 "SC 13".
                form_m = re.match(r"(SC 13[DG](?:/A)?)", t, re.IGNORECASE)
                if "(SUBJECT)" not in t.upper() or not form_m:
                    continue
                cik_m = re.search(r"\((\d{7,10})\)", t)
                comp_m = re.search(r"SC 13[DG](?:/A)?\s*-\s*(.+?)\s*\(", t, re.IGNORECASE)
                updated = re.search(r"<updated>(.*?)</updated>", e)
                link = re.search(r'href="([^"]+)"', e)
                raw.append({
                    "company": (comp_m.group(1) if comp_m else t)[:80],
                    "cik": int(cik_m.group(1)) if cik_m else None,
                    "form": form_m.group(1) if form_m else "SC 13",
                    "updated": (updated.group(1) if updated else "")[:25],
                    "url": link.group(1) if link else "",
                })
            CACHE_13DG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_13DG_FILE.write_text(json.dumps(raw, ensure_ascii=False))
        except Exception as e:
            logger.warning("[13DG] EDGAR getcurrent 拉取失败 (降级空): %s", e)
            return []

    try:
        from app.data.sec_form4_provider import SECForm4Provider
        cik2tk = SECForm4Provider()._cik_ticker_map or {}
    except Exception as e:
        logger.warning("[13DG] CIK 映射不可用 (降级空): %s", e)
        return []

    # 兼容旧缓存: 旧解析曾把 SC 13E3 收成 form="SC 13"; 只保留 13D/G.
    raw = [
        e for e in (raw or [])
        if str(e.get("form") or "").upper().startswith(("SC 13D", "SC 13G"))
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out = []
    for e in raw:
        tk = cik2tk.get(e.get("cik") or -1)
        if not tk:
            continue
        tk = str(tk).upper()
        if universe_tickers and tk not in universe_tickers:
            continue
        try:
            if datetime.fromisoformat(e["updated"].replace("Z", "+00:00")) < cutoff:
                continue
        except Exception:
            pass
        out.append({"ticker": tk, "company": e["company"], "form": e["form"],
                    "updated": e["updated"], "url": e["url"]})
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rows = fetch_recent_8k(lookback_hours=48)
    print(f"近48h 8-K (item 白名单+有 ticker): {len(rows)} 条")
    for r in rows[:10]:
        print(f"  {r['ticker']:<6} {r['updated']} {'; '.join(r['item_labels'])} — {r['company']}")
    rows2 = fetch_recent_13dg(lookback_hours=96)
    print(f"近96h SC 13D/G 举牌 (Subject+有 ticker): {len(rows2)} 条")
    for r in rows2[:10]:
        print(f"  {r['ticker']:<6} {r['form']:<9} {r['updated']} — {r['company']}")
