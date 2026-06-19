"""
SEC Form 4 内部人交易数据提供商 (ADR-007: 2026-06-16)

数据来源: SEC EDGAR EFTS API + Company Submissions API
限速: 10 req/s (SEC 官方限制)

核心逻辑:
  1. 通过 EFTS Search Index 发现近期 Form 4 提交
  2. 下载 Form 4 XML 解析内部人交易
  3. 识别集群买入信号 (30天内 ≥2 位不同内部人净买入)

内部人买入信号条件:
  - transactionCode = "P" (Open market Purchase)
  - 买入金额 > $50K (排除象征性买入)
  - 非 10b5-1 计划交易 (排除自动化交易)
  - 30天内 ≥2 位不同内部人买入 = 集群买入 (最强信号)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger("ftseek.insider_provider")

# ── 常量 ──
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "FTSeek-US contact@ftseek.ai")
EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_DIR = Path(os.getenv("FTSEEK_CACHE_DIR", "/opt/ftseek/FTSeek-US/data/sec_cache"))
CACHE_TTL = 3600  # 1 hour cache for discovery results
MIN_BUY_AMOUNT = 50_000  # $50K minimum for meaningful insider buys
CLUSTER_WINDOW_DAYS = 30  # 30-day window for cluster detection
REQUEST_DELAY = 0.12  # 10 req/s = 100ms, add 20ms safety margin


@dataclass
class InsiderTransaction:
    """内部人交易记录"""
    symbol: str               # 股票代码 (e.g., "AAPL")
    company_name: str         # 公司名称
    insider_name: str         # 内部人姓名
    insider_cik: str          # 内部人 CIK
    insider_role: str         # 角色 (Officer/Director/10%Owner)
    officer_title: str        # 具体职位 (CEO/CFO/etc)
    transaction_code: str     # P=买入, S=卖出
    transaction_date: str     # 交易日期 YYYY-MM-DD
    shares: float             # 股数
    price_per_share: float    # 每股价格
    dollar_amount: float      # 总金额
    shares_after: float       # 交易后持股
    ownership_type: str       # D=直接, I=间接
    filing_date: str          # 提交日期
    accession_number: str     # SEC accession number
    is_10b51: bool = False    # 是否为 10b5-1 计划交易


@dataclass
class InsiderClusterBuy:
    """内部人集群买入信号"""
    symbol: str
    company_name: str
    buyers: List[Dict]        # [{name, role, title, amount, date}, ...]
    total_amount: float       # 总买入金额
    unique_buyers: int        # 不同内部人数量
    first_buy_date: str       # 最早买入日期
    latest_buy_date: str      # 最近买入日期
    days_span: int            # 买入跨度天数
    score: float              # 信号强度 (0-1)
    rationale: str            # 人类可读的理由


class SECForm4Provider:
    """SEC Form 4 内部人交易数据提供商"""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._ticker_cik_map: Dict[str, int] = {}
        self._cik_ticker_map: Dict[int, str] = {}
        self._last_request_time = 0.0
        self._load_ticker_map()

    def _rate_limit(self):
        """SEC 限速: 10 req/s"""
        elapsed = time.time() - self._last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

    def _sec_request(self, url: str) -> Optional[bytes]:
        """发送 SEC API 请求 (带 User-Agent + 限速)"""
        self._rate_limit()
        req = Request(url, headers={"User-Agent": SEC_USER_AGENT})
        try:
            with urlopen(req, timeout=15) as resp:
                return resp.read()
        except (HTTPError, URLError) as e:
            logger.debug(f"[SEC] Request failed: {url[:80]}... -> {e}")
            return None

    def _load_ticker_map(self):
        """加载 ticker → CIK 映射"""
        cache_file = CACHE_DIR / "ticker_cik_map.json"
        # Use cache if < 7 days old
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 7 * 86400:
                try:
                    data = json.loads(cache_file.read_text())
                    self._ticker_cik_map = data.get("ticker_to_cik", {})
                    self._cik_ticker_map = {int(v): k for k, v in self._ticker_cik_map.items()}
                    logger.debug(f"[SEC] Loaded {len(self._ticker_cik_map)} ticker mappings from cache")
                    return
                except Exception:
                    pass

        # Fetch fresh
        raw = self._sec_request(SEC_TICKERS_URL)
        if raw:
            try:
                data = json.loads(raw)
                for _idx, info in data.items():
                    ticker = info.get("ticker", "").upper()
                    cik = int(info.get("cik_str", info.get("cik", 0)))
                    if ticker and cik:
                        self._ticker_cik_map[ticker] = cik
                        self._cik_ticker_map[cik] = ticker
                cache_file.write_text(json.dumps({"ticker_to_cik": self._ticker_cik_map}))
                logger.info(f"[SEC] Loaded {len(self._ticker_cik_map)} ticker→CIK mappings")
            except Exception as e:
                logger.warning(f"[SEC] Failed to parse ticker map: {e}")

    def get_cik(self, symbol: str) -> Optional[int]:
        """获取公司 CIK (symbol 格式: US.AAPL 或 AAPL)"""
        ticker = symbol.replace("US.", "").upper()
        return self._ticker_cik_map.get(ticker)

    def get_ticker(self, cik: int) -> Optional[str]:
        """从 CIK 获取 ticker"""
        return self._cik_ticker_map.get(cik)

    def fetch_company_form4s(self, symbol: str, max_age_days: int = 60) -> List[InsiderTransaction]:
        """
        获取指定公司近期 Form 4 交易

        Args:
            symbol: US.AAPL 格式
            max_age_days: 最大回溯天数

        Returns:
            内部人交易列表 (仅包含 P/S 交易, 排除衍生品)
        """
        ticker = symbol.replace("US.", "").upper()
        cik = self.get_cik(ticker)
        if not cik:
            logger.debug(f"[SEC] No CIK for {ticker}")
            return []

        # Check cache
        cache_key = f"form4_{cik}_{max_age_days}d"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        # Fetch company submissions
        cik_padded = str(cik).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        raw = self._sec_request(url)
        if not raw:
            return []

        try:
            data = json.loads(raw)
            company_name = data.get("name", ticker)
            filings = data.get("filings", {}).get("recent", {})
        except Exception as e:
            logger.warning(f"[SEC] Parse error for {ticker}: {e}")
            return []

        # Filter Form 4 within date range
        cutoff = datetime.now() - timedelta(days=max_age_days)
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accession_numbers = filings.get("accessionNumber", [])
        primary_docs = filings.get("primaryDocument", [])

        form4_indices = []
        for i, (form, date_str) in enumerate(zip(forms, dates)):
            if form == "4":
                try:
                    filing_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if filing_date >= cutoff:
                        form4_indices.append(i)
                except ValueError:
                    continue

        if not form4_indices:
            return []

        # Parse each Form 4 (limit to 20 most recent to avoid rate limiting)
        transactions = []
        for idx in form4_indices[:20]:
            acc_no = accession_numbers[idx]
            filing_date = dates[idx]
            cik_no_zeros = str(cik)

            # Build filing URL
            acc_no_clean = acc_no.replace("-", "")
            filing_url = f"{SEC_ARCHIVES_URL}/{cik_no_zeros}/{acc_no_clean}/{acc_no}.txt"

            txns = self._parse_form4_filing(
                filing_url, ticker, company_name, filing_date, acc_no
            )
            transactions.extend(txns)

        # Cache results
        self._write_cache(cache_key, transactions)
        logger.info(f"[SEC] {ticker}: {len(transactions)} insider transactions in {max_age_days}d")
        return transactions

    def _parse_form4_filing(
        self, url: str, symbol: str, company_name: str,
        filing_date: str, accession_number: str
    ) -> List[InsiderTransaction]:
        """解析单个 Form 4 提交"""
        raw = self._sec_request(url)
        if not raw:
            return []

        text = raw.decode("utf-8", errors="replace")

        # Extract XML from filing text
        xml_match = re.search(r"<XML>(.*?)</XML>", text, re.DOTALL)
        if not xml_match:
            # Try direct XML parse (some filings are pure XML)
            xml_text = text
        else:
            xml_text = xml_match.group(1)

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            # Strip XML declaration (common issue with SEC filings)
            xml_text_clean = re.sub(r'<\?xml[^?]*\?>', '', xml_text).strip()
            try:
                root = ET.fromstring(xml_text_clean)
            except ET.ParseError:
                logger.debug(f"[SEC] XML parse error for {symbol} {accession_number}")
                return []

        transactions = []

        # Extract reporting owner info
        owner = root.find(".//reportingOwner")
        if owner is None:
            return transactions

        owner_id = owner.find("reportingOwnerId")
        insider_cik = ""
        insider_name = ""
        if owner_id is not None:
            cik_el = owner_id.find("rptOwnerCik")
            name_el = owner_id.find("rptOwnerName")
            insider_cik = cik_el.text if cik_el is not None else ""
            insider_name = name_el.text if name_el is not None else ""

        # Determine role
        rel = owner.find("reportingOwnerRelationship")
        role = "Other"
        officer_title = ""
        if rel is not None:
            is_officer = rel.findtext("isOfficer", "0")
            is_director = rel.findtext("isDirector", "0")
            is_10pct = rel.findtext("isTenPercentOwner", "0")
            officer_title = rel.findtext("officerTitle", "")
            # SEC uses both "1"/"0" and "true"/"false"
            if is_officer in ("1", "true"):
                role = "Officer"
            elif is_director in ("1", "true"):
                role = "Director"
            elif is_10pct in ("1", "true"):
                role = "10%Owner"

        # Parse non-derivative transactions (actual stock buys/sells)
        for ndt in root.findall(".//nonDerivativeTransaction"):
            txn = self._extract_transaction(
                ndt, symbol, company_name, insider_name, insider_cik,
                role, officer_title, filing_date, accession_number
            )
            if txn:
                transactions.append(txn)

        # Also check nonDerivativeHolding for holdings context
        return transactions

    def _extract_transaction(
        self, elem, symbol, company_name, insider_name, insider_cik,
        role, officer_title, filing_date, accession_number
    ) -> Optional[InsiderTransaction]:
        """从 XML 元素提取交易数据"""
        # Transaction code
        coding = elem.find("transactionCoding")
        if coding is None:
            return None
        txn_code = coding.findtext("transactionCode", "")
        # Only care about P (purchase) and S (sale)
        if txn_code not in ("P", "S"):
            return None

        # Check for equity swap (exclude)
        swap = coding.findtext("equitySwapInvolved", "0")
        if swap == "1":
            return None

        # Transaction date
        date_elem = elem.find("transactionDate")
        txn_date = date_elem.findtext("value", filing_date) if date_elem is not None else filing_date

        # Amounts
        amounts = elem.find("transactionAmounts")
        if amounts is None:
            return None

        shares_str = amounts.findtext("transactionShares/value", "0") or "0"
        price_str = amounts.findtext("transactionPricePerShare/value", "0") or "0"
        acquired_code = amounts.findtext("transactionAcquiredDisposedCode/value", "")

        try:
            shares = float(shares_str)
            price = float(price_str)
        except (ValueError, TypeError):
            return None

        if shares <= 0 or price <= 0:
            return None

        dollar_amount = shares * price

        # Post-transaction holdings
        post = elem.find("postTransactionAmounts")
        shares_after = 0.0
        if post is not None:
            sa_str = post.findtext("sharesOwnedFollowingTransaction/value", "0") or "0"
            try:
                shares_after = float(sa_str)
            except (ValueError, TypeError):
                shares_after = 0.0

        # Ownership type
        ownership = elem.find("ownershipNature")
        own_type = "D"
        if ownership is not None:
            own_type = ownership.findtext("directOrIndirectOwnership/value", "D")

        return InsiderTransaction(
            symbol=symbol,
            company_name=company_name,
            insider_name=insider_name,
            insider_cik=insider_cik,
            insider_role=role,
            officer_title=officer_title,
            transaction_code=txn_code,
            transaction_date=txn_date,
            shares=shares,
            price_per_share=price,
            dollar_amount=dollar_amount,
            shares_after=shares_after,
            ownership_type=own_type,
            filing_date=filing_date,
            accession_number=accession_number,
        )

    def detect_cluster_buys(
        self, symbol: str, window_days: int = CLUSTER_WINDOW_DAYS
    ) -> Optional[InsiderClusterBuy]:
        """
        检测内部人集群买入信号

        集群买入条件:
          - 30天内 ≥2 位不同内部人净买入
          - 每位买入金额 > $50K
          - 非 10b5-1 计划交易

        Returns:
            InsiderClusterBuy 或 None
        """
        transactions = self.fetch_company_form4s(symbol, max_age_days=window_days + 10)

        # Filter: only purchases > $50K
        cutoff = datetime.now() - timedelta(days=window_days)
        buys = []
        for txn in transactions:
            if txn.transaction_code != "P":
                continue
            if txn.dollar_amount < MIN_BUY_AMOUNT:
                continue
            try:
                txn_date = datetime.strptime(txn.transaction_date, "%Y-%m-%d")
                if txn_date < cutoff:
                    continue
            except ValueError:
                continue
            buys.append(txn)

        if len(buys) < 2:
            return None

        # Group by unique insider
        by_insider: Dict[str, List[InsiderTransaction]] = {}
        for b in buys:
            key = b.insider_cik or b.insider_name
            if key not in by_insider:
                by_insider[key] = []
            by_insider[key].append(b)

        # Need ≥2 unique insiders
        if len(by_insider) < 2:
            return None

        # Build cluster signal
        buyer_details = []
        total_amount = 0.0
        all_dates = []

        for cik, txns in by_insider.items():
            insider_total = sum(t.dollar_amount for t in txns)
            total_amount += insider_total
            for t in txns:
                all_dates.append(t.transaction_date)
            buyer_details.append({
                "name": txns[0].insider_name,
                "role": txns[0].insider_role,
                "title": txns[0].officer_title,
                "amount": insider_total,
                "date": txns[-1].transaction_date,
                "shares": sum(t.shares for t in txns),
            })

        all_dates.sort()
        first_date = all_dates[0]
        last_date = all_dates[-1]
        try:
            d1 = datetime.strptime(first_date, "%Y-%m-%d")
            d2 = datetime.strptime(last_date, "%Y-%m-%d")
            days_span = (d2 - d1).days
        except ValueError:
            days_span = 0

        # Score: based on number of buyers, total amount, recency
        n_buyers = len(by_insider)
        amount_score = min(1.0, total_amount / 500_000)  # $500K = max score
        buyer_score = min(1.0, (n_buyers - 1) / 3)  # 4+ buyers = max score
        recency_score = 1.0 - (days_span / window_days)  # Tighter cluster = higher score
        score = 0.3 * amount_score + 0.4 * buyer_score + 0.3 * recency_score
        score = round(min(1.0, max(0.0, score)), 3)

        ticker = symbol.replace("US.", "")
        buyer_strs = []
        for b in buyer_details:
            buyer_strs.append(f"{b['name']}({b['role']})${b['amount']:,.0f}")
        rationale = (
            f"{n_buyers} insiders bought ${total_amount:,.0f} of {ticker} "
            f"in {days_span}d: " + ", ".join(buyer_strs)
        )

        return InsiderClusterBuy(
            symbol=symbol,
            company_name=buys[0].company_name,
            buyers=buyer_details,
            total_amount=total_amount,
            unique_buyers=n_buyers,
            first_buy_date=first_date,
            latest_buy_date=last_date,
            days_span=days_span,
            score=score,
            rationale=rationale,
        )

    def scan_watchlist(self, symbols: List[str]) -> List[InsiderClusterBuy]:
        """
        扫描 watchlist 中所有标的的内部人集群买入

        Args:
            symbols: ["US.AAPL", "US.NVDA", ...] 格式

        Returns:
            所有检测到集群买入的信号列表
        """
        clusters = []
        for sym in symbols:
            try:
                cluster = self.detect_cluster_buys(sym)
                if cluster:
                    clusters.append(cluster)
                    logger.info(
                        f"[Insider] 🟢 {sym} cluster buy: "
                        f"{cluster.unique_buyers} buyers, ${cluster.total_amount:,.0f}"
                    )
            except Exception as e:
                logger.debug(f"[Insider] {sym} scan error: {e}")
            # Rate limit between symbols
            time.sleep(0.15)

        return clusters

    # ── 缓存 ──

    def _cache_path(self, key: str) -> Path:
        safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
        return CACHE_DIR / f"{safe_key}.json"

    def _read_cache(self, key: str):
        path = self._cache_path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > CACHE_TTL:
            return None
        try:
            data = json.loads(path.read_text())
            return data
        except Exception:
            return None

    def _write_cache(self, key: str, data):
        path = self._cache_path(key)
        try:
            # Serialize InsiderTransaction objects
            if isinstance(data, list) and data and isinstance(data[0], InsiderTransaction):
                serialized = [
                    {
                        "symbol": t.symbol, "company_name": t.company_name,
                        "insider_name": t.insider_name, "insider_cik": t.insider_cik,
                        "insider_role": t.insider_role, "officer_title": t.officer_title,
                        "transaction_code": t.transaction_code,
                        "transaction_date": t.transaction_date,
                        "shares": t.shares, "price_per_share": t.price_per_share,
                        "dollar_amount": t.dollar_amount,
                        "shares_after": t.shares_after,
                        "ownership_type": t.ownership_type,
                        "filing_date": t.filing_date,
                        "accession_number": t.accession_number,
                    }
                    for t in data
                ]
                path.write_text(json.dumps(serialized))
            else:
                path.write_text(json.dumps(data))
        except Exception as e:
            logger.debug(f"[SEC] Cache write failed: {e}")


# Singleton
_instance: Optional[SECForm4Provider] = None


def get_sec_form4_provider() -> SECForm4Provider:
    global _instance
    if _instance is None:
        _instance = SECForm4Provider()
    return _instance
