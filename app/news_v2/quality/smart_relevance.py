"""
SmartRelevanceEngine — 上下文消歧 + 板块传导 + 综合评分 (N8/N9, 2026-05-14)

核心设计 (4 层智能 funnel):

L1 候选抽取 (NER-lite):
- 用 TickerDictionary 反查命中 ticker / 公司名 / 中文别名
- 输出 candidate_set + match_evidence

L2 上下文消歧 (核心改进):
- 判断"苹果"是 AAPL 还是水果:
  - 命中 product_word ("iPhone"/"Mac"/"Cook") → 确证, 高分
  - 仅命中名字 → 低分
- 解决纯关键词无法解决的歧义

L3 板块联动:
- 复用现有 RelevanceFilter.GEO_TRANSMISSION
- 一个 anchor (TSMC) 涨跌触发关联 ticker (NVDA/AMD)

L4 综合评分:
- 0-1 normalized
- 含 evidence 链 (可审计 = 比 LLM 黑盒可控)

可控:
- 纯 Python, 0 新依赖
- 每条评分有 evidence 链, 可调试
- yaml 配置阈值 (默认值合理)
- 失败降级到现有 RelevanceFilter
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TickerMatch:
    """单个 ticker 的匹配结果"""
    ticker: str
    score: float                       # 0-1 该 ticker 与新闻相关性
    name_hit: bool = False             # 命中 ticker / 名称
    signal_words_hit: List[str] = field(default_factory=list)  # 命中的 product/person 等
    is_disambiguated: bool = False     # 是否做了上下文消歧
    sector: str = ""

    @property
    def has_strong_evidence(self) -> bool:
        """强证据 = 名字 + 至少 1 个信号词 (消歧通过)"""
        return self.name_hit and len(self.signal_words_hit) > 0


@dataclass
class RelevanceResult:
    """新闻整体相关性"""
    overall_score: float = 0.0         # 0-1 整体分数
    primary_tickers: List[str] = field(default_factory=list)
    related_tickers: List[str] = field(default_factory=list)  # 通过板块传导关联
    matches: Dict[str, TickerMatch] = field(default_factory=dict)
    macro_hit: bool = False             # 命中宏观词 (CPI/Fed/FOMC)
    sectors: List[str] = field(default_factory=list)
    is_relevant: bool = False           # 综合判定
    rejection_reasons: List[str] = field(default_factory=list)

    def to_log(self) -> str:
        return (
            f"score={self.overall_score:.2f} "
            f"primary={self.primary_tickers} related={self.related_tickers} "
            f"macro={self.macro_hit} sectors={self.sectors}"
        )


# ─── 歧义词集 (这些词做名字时容易被误识别) ─────────────────
AMBIGUOUS_NAMES = {
    "苹果",   # 苹果公司 / 水果
    "亚马逊", # 亚马逊公司 / 流域
    "Apple", # 同
    "Meta",  # Meta 公司 / 元
    "甲骨文",# Oracle / 古代占卜
    "微软",   # Microsoft / 软件公司 (相对少歧义)
    "中国",   # 不是 ticker, 但常误命中 China ADR
    "美国",
}

# ─── 财经语境词 (任何 ticker name 在这些词附近 → 视为消歧通过) ────
# 即使该 ticker 没有自己的产品词命中, 出现在财经语境也确证是公司不是水果
FINANCIAL_CONTEXT_KEYWORDS = {
    # 中文
    "财报", "EPS", "营收", "净利", "毛利", "指引", "回购", "并购",
    "盘前", "盘后", "股价", "做空", "做多", "期权", "评级", "上调",
    "下调", "目标价", "市值", "涨停", "跌停", "复牌", "停牌", "拆股",
    "分红", "派息", "ADR", "美股", "纳指", "道指", "标普", "盈利",
    # 英文
    "earnings", "revenue", "guidance", "buyback", "merger",
    "premarket", "afterhours", "rating", "upgrade", "downgrade",
    "price target", "market cap", "halted", "split", "dividend",
}


class SmartRelevanceEngine:
    """智能新闻相关性引擎 (替代纯关键词)"""

    def __init__(
        self,
        watchlist_codes: Optional[Set[str]] = None,
        macro_keywords: Optional[List[str]] = None,
    ):
        self._watchlist = watchlist_codes or set()
        # 复用现有 macro keywords + 加中文宏观词
        _CN_MACRO = [
            "美联储", "鲍威尔", "美联储议息", "FOMC", "美元",
            "纳指", "标普", "道指", "标普500", "纳斯达克",
            "通胀", "CPI", "PPI", "非农", "失业率", "GDP",
            "降息", "加息", "维持利率", "缩表", "扩表",
            "VIX", "美债", "10年期国债", "美元指数",
            "贸易战", "关税", "制裁", "中美关系",
        ]
        try:
            from app.news_v2.quality.relevance_filter import RelevanceFilter
            self._macro_kw = list(set(macro_keywords or []) | set(RelevanceFilter.MACRO_KEYWORDS) | set(_CN_MACRO))
            self._geo_transmission = RelevanceFilter.GEO_TRANSMISSION
        except Exception:
            self._macro_kw = list(set(macro_keywords or []) | set(_CN_MACRO))
            self._geo_transmission = {}

        # 歧义词阈值: 单纯命中名字 → 0.3 分; 配合信号词 → 0.8+
        self.ambiguous_name_score = 0.3
        self.unambiguous_name_score = 0.6
        self.signal_word_bonus = 0.4
        self.macro_bonus = 0.35
        self.watchlist_bonus = 0.2

    def update_watchlist(self, watchlist_codes: Set[str]):
        self._watchlist = set(watchlist_codes)

    def evaluate(self, title: str, content: str = "") -> RelevanceResult:
        """
        主入口: 给一条新闻评估相关性 + 抽取 ticker.

        Returns: RelevanceResult (含完整 evidence)
        """
        from app.news_v2.quality.ticker_dictionary import get_ticker_dictionary
        td = get_ticker_dictionary()

        text = f"{title}\n{content}".strip()
        if not text:
            return RelevanceResult()

        text_upper = text.upper()
        result = RelevanceResult()
        matches: Dict[str, TickerMatch] = {}

        # ── L1: 名字命中 (initial candidates) ───────────────
        # a) 直接 ticker (大写 2-5 字母 word boundary)
        for m in re.finditer(r"\b([A-Z]{2,5})\b", text):
            tk = m.group(1)
            if td.get(tk):
                self._record_match(matches, tk, name_hit=True, td=td)

        # b) 名称/别名 (各 ticker 的 name_variants)
        for tk in td.all_tickers():
            ent = td.get(tk)
            if ent is None:
                continue
            for variant in ent.all_name_variants():
                if not variant:
                    continue
                # 中文 case-sensitive, 英文 case-insensitive
                hit = (variant in text) or (
                    all(ord(c) < 128 for c in variant) and variant.upper() in text_upper
                )
                if hit:
                    self._record_match(matches, tk, name_hit=True, td=td)
                    break

        # ── L2: 上下文消歧 — 检查每个 candidate 的 signal_words ──
        for tk, mat in matches.items():
            ent = td.get(tk)
            if not ent:
                continue
            for sw in ent.all_signal_words():
                if not sw:
                    continue
                if sw in text or (all(ord(c) < 128 for c in sw) and sw.upper() in text_upper):
                    mat.signal_words_hit.append(sw)
            # 计算分数
            mat.score = self._compute_match_score(mat, ent)
            mat.is_disambiguated = mat.has_strong_evidence

        # ── 检查财经语境词 (任何 ticker 命中 + 周围有财经词 → 视为消歧) ──
        finance_context_present = any(
            kw in text or (all(ord(c) < 128 for c in kw) and kw.upper() in text_upper)
            for kw in FINANCIAL_CONTEXT_KEYWORDS
        )

        # ── 处理歧义名字 — 单纯名字命中但无信号词 → 降级 ──
        for tk, mat in list(matches.items()):
            ent = td.get(tk)
            if not ent:
                continue
            ambiguous = any(n in AMBIGUOUS_NAMES for n in ent.all_name_variants() if n in text)
            if ambiguous and not mat.signal_words_hit:
                if finance_context_present:
                    # 财经语境 → 视为消歧通过. 重算分数: 不再按歧义名字基础分, 按 unambiguous + signal
                    mat.signal_words_hit.append("[financial_context]")
                    # 重新算分: 强制按 unambiguous 计 (财经语境已确证不是歧义)
                    base = self.unambiguous_name_score
                    sig_bonus = min(self.signal_word_bonus, 0.15 * len(mat.signal_words_hit))
                    wl_bonus = self.watchlist_bonus if (
                        f"US.{mat.ticker}" in self._watchlist or mat.ticker in self._watchlist
                    ) else 0.0
                    mat.score = min(1.0, base + sig_bonus + wl_bonus)
                    mat.is_disambiguated = True
                    logger.debug(f"[SmartRelevance] {tk} 财经语境消歧通过, score={mat.score:.2f}")
                else:
                    # 歧义且无信号词且无财经语境 → 大幅降分
                    mat.score *= 0.3
                    logger.debug(f"[SmartRelevance] {tk} 歧义降级 (无消歧线索)")

        # ── L3: 板块传导 — 通过宏观/地缘事件关联 tickers ──
        related: Set[str] = set()
        for path_name, path_info in self._geo_transmission.items():
            for kw in path_info.get("keywords", []):
                if kw.lower() in text.lower():
                    # beneficiaries / victims 加入 related
                    for tk in (path_info.get("beneficiaries", []) + path_info.get("victims", [])):
                        if tk not in matches and td.get(tk):
                            related.add(tk)
                    break

        # ── 宏观词 ──
        for kw in self._macro_kw:
            if kw.lower() in text.lower():
                result.macro_hit = True
                break

        # ── 综合评分 ──
        # 主要 ticker = score >= 0.5
        primary = [tk for tk, m in matches.items() if m.score >= 0.5]
        # 弱命中 ticker (0.3-0.5)
        weak = [tk for tk, m in matches.items() if 0.3 <= m.score < 0.5]

        result.matches = matches
        result.primary_tickers = sorted(primary, key=lambda t: -matches[t].score)[:5]
        result.related_tickers = sorted(related)[:5]
        result.sectors = list({matches[tk].sector for tk in primary if matches[tk].sector})

        # 整体分数: max(primary scores) + macro bonus + watchlist bonus
        max_match = max((m.score for m in matches.values()), default=0.0)
        watchlist_hit = any(f"US.{tk}" in self._watchlist or tk in self._watchlist
                           for tk in primary)
        score = max_match
        if result.macro_hit:
            score = min(1.0, score + 0.35)
        if watchlist_hit:
            score = min(1.0, score + self.watchlist_bonus)

        result.overall_score = round(score, 3)
        result.is_relevant = score >= 0.4 or result.macro_hit or len(result.primary_tickers) > 0

        if not result.is_relevant:
            if not matches and not result.macro_hit:
                result.rejection_reasons.append("no_ticker_no_macro")
            elif weak:
                result.rejection_reasons.append(f"only_weak_match: {weak}")

        return result

    def _record_match(self, matches: Dict[str, TickerMatch], ticker: str,
                      name_hit: bool = False, td=None):
        if ticker not in matches:
            sector = ""
            if td:
                ent = td.get(ticker)
                sector = ent.sector if ent else ""
            matches[ticker] = TickerMatch(ticker=ticker, score=0.0, name_hit=False, sector=sector)
        if name_hit:
            matches[ticker].name_hit = True

    def _compute_match_score(self, m: TickerMatch, ent) -> float:
        """单 ticker 综合评分"""
        s = 0.0
        if m.name_hit:
            # 歧义名字基础分低, 必须配合信号词
            is_ambig = any(n in AMBIGUOUS_NAMES for n in ent.all_name_variants())
            s += self.ambiguous_name_score if is_ambig else self.unambiguous_name_score
        if m.signal_words_hit:
            # 信号词命中数加分 (上限 +0.4)
            s += min(self.signal_word_bonus, 0.15 * len(m.signal_words_hit))
        # watchlist 加成
        if (f"US.{m.ticker}" in self._watchlist) or (m.ticker in self._watchlist):
            s += self.watchlist_bonus
        return min(1.0, s)


_singleton: Optional[SmartRelevanceEngine] = None


def get_smart_relevance(watchlist: Optional[Set[str]] = None) -> SmartRelevanceEngine:
    """单例. watchlist 变化时调 update_watchlist"""
    global _singleton
    if _singleton is None:
        _singleton = SmartRelevanceEngine(watchlist_codes=watchlist)
    return _singleton
