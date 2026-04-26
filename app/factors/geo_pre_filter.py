"""
FTSeek 地缘政治事件预筛器 (GeoEventPreFilter)

设计灵感: QuantDinger 的地缘政治关键词匹配逻辑
改造适配: FTSeek 的新闻管线 + LLM 推理管线

核心原则:
1. 预筛只做"标记"，不做"判断" — 最终判断交给 LLM
2. 分级标记: severe (严重) / moderate (中度) / none (无)
3. 中英文双语覆盖
4. 零 LLM 消耗，纯正则匹配

用法:
    from app.factors.geo_pre_filter import GeoEventPreFilter
    prefilter = GeoEventPreFilter()
    result = prefilter.scan("伊朗达成停火协议", "中东局势缓和...")
    # result: {"severity": "moderate", "matched_patterns": [...], "context_flags": []}
"""

import re
from typing import Dict, List, Tuple, Optional
import logging

logger = logging.getLogger("ftseek.factors.geo_pre_filter")


class GeoEventPreFilter:
    """轻量级地缘政治事件预筛器 — 纯正则，不调 LLM"""

    SEVERE_PATTERNS_EN = [
        r"\b(?:war|wars|warfare|wartime)\b",
        r"\b(?:invasion|invaded|invading|invade)\b",
        r"\b(?:airstrike|air\s*strikes?|missile\s+strike|drone\s+strike)\b",
        r"\b(?:military\s+attack|armed\s+attack|troops?\s+(?:fire|attack|invade))\b",
        r"\b(?:declare[sd]?\s+war|state\s+of\s+war|act\s+of\s+war)\b",
        r"\b(?:martial\s+law|military\s+coup|coup\s+d\'[\u2019]?etat)\b",
        r"\b(?:terror(?:ist)?\s+attack|mass\s+shooting\s+at)\b",
    ]

    SEVERE_PATTERNS_ZH = [
        "宣战", "战争爆发", "全面战争", "武装冲突", "军事打击",
        "军事入侵", "空袭", "导弹袭击", "开战", "交火", "战火",
    ]

    MODERATE_PATTERNS_EN = [
        r"\bgeopolitical\b",
        r"\b(?:armed|military)\s+conflict\b",
        r"\b(?:international\s+)?sanctions?\s+(?:on|against|targeting|hit)\b",
        r"\b(?:naval\s+blockade|border\s+clash|ceasefire\s+(?:broken|violated))\b",
        r"\b(?:evacuat\w+\s+(?:the\s+)?embassy|embassy\s+evacuation)\b",
        r"\b(?:nuclear\s+(?:threat|strike|weapon)|nuclear\s+war)\b",
    ]

    MODERATE_PATTERNS_ZH = [
        "地缘政治危机", "国际制裁升级", "断交", "撤侨", "军事对峙",
        "地区冲突升级",
        # 正面地缘事件 — 预筛标记为 moderate，LLM 判断实际是利好
        "停火", "和平协议", "和谈", "缓和", "停战",
    ]

    REGION_CONFLICT_EN = [
        r"\b(?:russia|ukraine|iran|israel|gaza|hamas|taiwan|north\s+korea|dprk|syria|yemen|lebanon|nato)\b.{0,40}\b(?:invade|attack|strike|war|conflict|sanction)\b",
        r"\b(?:invade|attack|strike|war|conflict|sanction)\b.{0,40}\b(?:russia|ukraine|iran|israel|gaza|hamas|taiwan|north\s+korea|dprk|syria|nato)\b",
    ]

    CONTEXT_FLAGS = {
        "停火": "ceasefire_positive",
        "和平协议": "peace_deal_positive",
        "和解": "reconciliation_positive",
        "ceasefire": "ceasefire_positive",
        "peace deal": "peace_deal_positive",
        "sanction": "sanction_negative",
        "制裁": "sanction_negative",
    }

    def __init__(self):
        self._severe_en = [re.compile(p, re.I) for p in self.SEVERE_PATTERNS_EN]
        self._moderate_en = [re.compile(p, re.I) for p in self.MODERATE_PATTERNS_EN]
        self._region_en = [re.compile(p, re.I) for p in self.REGION_CONFLICT_EN]

    def scan(self, title: str, content: str = "") -> Dict:
        combined = f"{title} {content}"
        combined_lower = combined.lower()

        severity = "none"
        matched = []
        flags = []

        for pat in self._severe_en:
            m = pat.search(combined_lower)
            if m:
                severity = "severe"
                matched.append(m.group())

        for kw in self.SEVERE_PATTERNS_ZH:
            if kw in combined:
                severity = "severe"
                matched.append(kw)

        for pat in self._region_en:
            m = pat.search(combined_lower)
            if m:
                severity = "severe"
                matched.append(m.group()[:50] + "...")

        if severity == "none":
            for pat in self._moderate_en:
                m = pat.search(combined_lower)
                if m:
                    severity = "moderate"
                    matched.append(m.group())

            for kw in self.MODERATE_PATTERNS_ZH:
                if kw in combined:
                    severity = "moderate"
                    matched.append(kw)

        for kw, flag in self.CONTEXT_FLAGS.items():
            if kw.lower() in combined_lower or kw in combined:
                flags.append(flag)

        return {
            "severity": severity,
            "matched_patterns": list(set(matched)),
            "context_flags": list(set(flags)),
            "is_geo_event": severity != "none",
        }

    def format_for_llm(self, scan_result: Dict) -> str:
        if not scan_result["is_geo_event"]:
            return ""

        lines = [
            "\u26a0\ufe0f \u9884\u7b5b\u6807\u8bb0: \u672c\u65b0\u95fb\u89e6\u53d1\u5730\u7f18\u653f\u6cbb\u4e8b\u4ef6\u68c0\u6d4b",
            "  \u4e25\u91cd\u7a0b\u5ea6: " + scan_result["severity"].upper(),
            "  \u5339\u914d\u5173\u952e\u8bcd: " + ", ".join(scan_result["matched_patterns"]),
        ]

        if scan_result["context_flags"]:
            lines.append("  \u4e0a\u4e0b\u6587\u6807\u8bb0: " + ", ".join(scan_result["context_flags"]))
            lines.append("  \u2192 \u8bf7\u7ed3\u5408\u4e0a\u4e0b\u6587\u5224\u65ad: \u8fd9\u662f\u5229\u597d(\u505c\u706b/\u548c\u5e73)\u8fd8\u662f\u5229\u7a7a(\u5236\u88c1/\u51b2\u7a81)")

        return "\n".join(lines)
