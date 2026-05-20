"""
FTSeek-US 地缘政治事件预筛器 (GeoEventPreFilter)

设计灵感: QuantDinger 地缘政治关键词 + FTSeek 港股预筛器
适配: 美股市场特有传导路径 (oil→energy stocks, china→semis, fed→rates)

核心原则:
1. 预筛只做"标记"，不做"判断" — 最终判断交给 LLM
2. 分级标记: severe (严重) / moderate (中度) / none (无)
3. 纯英文正则匹配 (美股新闻以英文为主)
4. 零 LLM 消耗，纯正则匹配
5. 内置美股传导路径映射

用法:
    from app.factors.geo_pre_filter import GeoEventPreFilter
    prefilter = GeoEventPreFilter()
    result = prefilter.scan("Iran ceasefire deal reached", "Oil prices drop 3%...")
"""

import re
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("ftseek_us.factors.geo_pre_filter")

# 美股传导路径映射: 地缘事件 → 受益股/受害股
US_TRANSMISSION_PATHS = {
    "oil_surge": {
        "trigger_patterns": [
            r"\boil\s+price|crude\s+surge|opec\s+cut|middle\s+east\s+conflict|iran\s+sanction",
            r"persian\s+gulf|strait\s+of\s+hormuz|pipeline\s+attack|oil\s+supply\s+disrupt",
            r"\b油价上涨|\b原油供应中断|\b中东冲突",
        ],
        "beneficiaries": ["XOM", "CVX", "COP", "SLB", "OXY", "HAL", "MPC", "VLO"],
        "victims": ["DAL", "UAL", "LUV", "AAL", "JBLU", "SAVE", "AMZN", "FDX", "UPS"],
        "sector_impact": {"Energy": "bullish", "Airlines": "bearish", "Transportation": "bearish"},
    },
    "oil_decrease": {
        "trigger_patterns": [
            r"ceasefire|peace\s+deal|iran\s+deal|opec\s+increase|oil\s+glut|oil\s+oversupply",
            r"middle\s+east\s+tension.*ease|tension.*de-escalat",
            r"\b停火\b|\b和平协议\b|\b油价下跌\b|\b和解\b|\b中东.*缓和\b|\b局势缓和\b",
        ],
        "beneficiaries": ["DAL", "UAL", "LUV", "AAL", "AMZN", "FDX", "UPS", "WMT", "TGT"],
        "victims": ["XOM", "CVX", "COP", "OXY", "SLB"],
        "sector_impact": {"Energy": "bearish", "Airlines": "bullish", "Consumer": "bullish"},
    },
    "china_tension": {
        "trigger_patterns": [
            r"taiwan\s+(?:strait|conflict|invasion|tension|crisis)",
            r"china\s+(?:sanction|restrict|ban|tension|trade\s+war)",
            r"south\s+china\s+sea|semiconductor\s+export\s+restrict|chip\s+war",
            r"taiwan.*militar|militar.*taiwan",
            r"\b台湾\b|\b台海\b|\b中美贸易\b|\b芯片制裁\b|\b半导体出口限制\b",
            r"\b台海.*演习\b|\b台海.*军演\b|\b台海.*紧张\b|\b台湾.*军事\b",
        ],
        "beneficiaries": ["INTC", "QCOM", "MU", "TXN", "AMAT", "LRCX", "KLAC"],
        "victims": ["BABA", "PDD", "JD", "NIO", "TSM", "BIDU", "LI", "XPEV"],
        "sector_impact": {"Semiconductors": "mixed", "China_ADR": "bearish"},
    },
    "fed_policy": {
        "trigger_patterns": [
            r"federal\s+reserve|fomc|jerome\s+powell|powell\s+speaks",
            r"rate\s+hike|rate\s+cut|interest\s+rate\s+decision",
            r"quantitative\s+(?:tightening|easing)|balance\s+sheet|qt|qe",
            r"美联储|加息|降息|利率决议|鲍威尔",
        ],
        "affects_all": True,
        "rate_sensitive_sectors": ["Technology", "Real_Estate", "Financials"],
    },
    "geopolitical_crisis": {
        "trigger_patterns": [
            r"war|invasion|military\s+attack|airstrike|missile\s+strike",
            r"nuclear\s+threat|nuclear\s+weapon|nuclear\s+war",
            r"martial\s+law|military\s+coup|terror(?:ist)?\s+attack",
            r"\b战争\b|\b军事入侵\b|\b空袭\b|\b核威胁\b|\b政变\b",
        ],
        "beneficiaries": ["LMT", "RTX", "NOC", "GD", "GLD", "SLV"],
        "victims": ["SPY", "QQQ", "IWM", "EFA", "EEM"],
        "sector_impact": {"Defense": "bullish", "Gold": "bullish", "Broad_Market": "bearish"},
    },
    "trade_policy": {
        "trigger_patterns": [
            r"tariff|trade\s+war|trade\s+deal|import\s+tariff|export\s+control",
            r"usmca|nafta|wto\s+dispute|section\s+301|section\s+232",
            r"\b关税\b|\b贸易战\b|\b出口管制\b",
        ],
        "affects_all": True,
    },
}


class GeoEventPreFilter:
    """轻量级地缘政治事件预筛器 — 纯正则，不调 LLM"""

    # === 严重级 (Severe) — 立即处理 ===
    SEVERE_PATTERNS = [
        r"\b(?:war|wars|warfare|wartime)\b",
        r"\b(?:invasion|invaded|invading|invade)\b",
        r"\b(?:airstrike|air\s*strikes?|missile\s+strike|drone\s+strike)\b",
        r"\b(?:military\s+attack|armed\s+attack|troops?\s+(?:fire|attack|invade))\b",
        r"\b(?:declare[sd]?\s+war|state\s+of\s+war|act\s+of\s+war)\b",
        r"\b(?:martial\s+law|military\s+coup|coup\s+d'[é']?etat)\b",
        r"\b(?:terror(?:ist)?\s+attack|mass\s+shooting\s+at)\b",
        r"\b(?:nuclear\s+(?:threat|strike|weapon)|nuclear\s+war)\b",
        # 中文
        "宣战", "战争爆发", "全面战争", "武装冲突", "军事打击",
        "军事入侵", "空袭", "导弹袭击", "开战", "交火", "战火",
        "核威胁", "核武器", "政变", "戒严",
    ]

    # === 中度级 (Moderate) — 需要关注 ===
    MODERATE_PATTERNS = [
        r"\bgeopolitical\b",
        r"\b(?:armed|military)\s+conflict\b",
        r"\bmiddle\s+east\s+(?:conflict|crisis|tension)\b",
        r"\b(?:international\s+)?sanctions?\s+(?:on|against|targeting|hit)\b",
        r"\b(?:naval\s+blockade|border\s+clash)\b",
        r"\b(?:evacuat\w+\s+(?:the\s+)?embassy|embassy\s+evacuation)\b",
        r"\b(?:ceasefire\s+(?:broken|violated))\b",
        r"\btension\s+(?:escalat|rises|surge)\b",
        r"\border\s+dispute\b",
        # 正面事件 — 预筛标记为 moderate，LLM 判断实际影响
        r"\bceasefire\s+deal\b", r"\bpeace\s+agreement\b",
        r"\bpeace\s+talks?\b", r"\bde-escalat",
        r"\bsanction\s+lift(?:ed|s)?\b", r"\btrade\s+deal\b",
        # 中文
        "地缘政治危机", "国际制裁升级", "断交", "撤侨", "军事对峙",
        "地区冲突升级", "中东冲突",
        "停火", "和平协议", "和谈", "缓和", "停战",
        "解除制裁", "贸易协议",
    ]

    # 区域冲突关键词
    REGION_KEYWORDS = [
        "russia", "ukraine", "iran", "israel", "gaza", "hamas",
        "taiwan", "north korea", "dprk", "syria", "yemen",
        "lebanon", "nato", "south china sea", "persian gulf",
        "red sea", "houthi", "hezbollah",
        "俄罗斯", "乌克兰", "伊朗", "以色列", "加沙",
        "台湾", "台海", "朝鲜", "叙利亚", "红海", "胡塞",
    ]

    # 上下文标记
    CONTEXT_FLAGS = {
        "停火": "ceasefire_positive",
        "和平协议": "peace_deal_positive",
        "和解": "reconciliation_positive",
        "缓和": "de_escalation_positive",
        "ceasefire": "ceasefire_positive",
        "peace deal": "peace_deal_positive",
        "peace agreement": "peace_deal_positive",
        "peace talks": "peace_talks_positive",
        "sanction": "sanction_negative",
        "制裁": "sanction_negative",
        "tariff": "tariff_negative",
        "关税": "tariff_negative",
    }

    def __init__(self):
        # 预编译正则
        self._severe_en = [re.compile(p, re.I) for p in self.SEVERE_PATTERNS if not any(c > '\u007f' for c in p)]
        self._moderate_en = [re.compile(p, re.I) for p in self.MODERATE_PATTERNS if not any(c > '\u007f' for c in p)]

        # 中文关键词列表
        self._severe_zh = [p for p in self.SEVERE_PATTERNS if any(c > '\u007f' for c in p)]
        self._moderate_zh = [p for p in self.MODERATE_PATTERNS if any(c > '\u007f' for c in p)]

    def scan(self, title: str, content: str = "") -> Dict:
        """
        扫描新闻标题和内容，标记地缘政治事件
        
        Args:
            title: 新闻标题
            content: 新闻内容 (可选)
        
        Returns:
            {
                "severity": "severe" | "moderate" | "none",
                "matched_patterns": ["keyword1", "keyword2"],
                "context_flags": ["ceasefire_positive"],
                "transmission_paths": ["oil_surge", "china_tension"],
                "is_geo_event": True/False,
            }
        """
        combined = f"{title} {content}"
        combined_lower = combined.lower()

        severity = "none"
        matched = []
        flags = []
        transmission_paths = []

        # Step 1: Severe English patterns
        for pat in self._severe_en:
            m = pat.search(combined_lower)
            if m:
                severity = "severe"
                matched.append(m.group())

        # Step 2: Severe Chinese keywords
        for kw in self._severe_zh:
            if kw in combined:
                severity = "severe"
                matched.append(kw)

        # Step 3: Moderate (only if not already severe)
        if severity == "none":
            for pat in self._moderate_en:
                m = pat.search(combined_lower)
                if m:
                    severity = "moderate"
                    matched.append(m.group())

            for kw in self._moderate_zh:
                if kw in combined:
                    severity = "moderate"
                    matched.append(kw)

        # Step 4: Context flags
        for kw, flag in self.CONTEXT_FLAGS.items():
            if kw.lower() in combined_lower or kw in combined:
                flags.append(flag)

        # Step 5: 传导路径匹配
        for path_name, path_info in US_TRANSMISSION_PATHS.items():
            for pattern in path_info["trigger_patterns"]:
                if re.search(pattern, combined_lower, re.I):
                    transmission_paths.append(path_name)
                    break

        return {
            "severity": severity,
            "matched_patterns": list(set(matched)),
            "context_flags": list(set(flags)),
            "transmission_paths": transmission_paths,
            "is_geo_event": severity != "none" or len(transmission_paths) > 0,
        }

    def format_for_llm(self, scan_result: Dict) -> str:
        """
        格式化预筛结果为 LLM prompt 段落
        
        用于注入到新闻分析 prompt 中。
        """
        if not scan_result.get("is_geo_event"):
            return ""

        lines = [
            "## ⚠️ 地缘政治事件检测 (预筛标记)",
            "",
            "预筛器已标记本新闻为地缘政治相关事件:",
            f"- **严重程度**: {scan_result['severity'].upper()}",
            f"- **匹配关键词**: {', '.join(scan_result['matched_patterns'])}",
        ]

        if scan_result.get("context_flags"):
            lines.append(f"- **上下文标记**: {', '.join(scan_result['context_flags'])}")
            lines.append("- **重要**: 预筛器只做关键词匹配，不理解语义。")
            lines.append("- 例如 \"停火/和平协议\" 会被标记为 moderate，但实际是**利好**（降低不确定性、提振风险资产）。")
            lines.append("- **你必须基于完整新闻内容做出最终判断，不要受预筛标记误导。**")
        else:
            lines.append("- **重要**: 你必须基于完整新闻内容做出最终判断。")

        if scan_result.get("transmission_paths"):
            lines.append("")
            lines.append("## 📡 美股传导路径检测")
            lines.append("")
            lines.append("预筛器检测到以下美股传导路径:")
            for path_name in scan_result["transmission_paths"]:
                path_info = US_TRANSMISSION_PATHS.get(path_name, {})
                lines.append(f"### {path_name}")
                if path_info.get("beneficiaries"):
                    lines.append(f"- **受益方**: {', '.join(path_info['beneficiaries'][:8])}")
                if path_info.get("victims"):
                    lines.append(f"- **受害方**: {', '.join(path_info['victims'][:8])}")
                if path_info.get("affects_all"):
                    lines.append("- **全市场影响**: 是")
                if path_info.get("sector_impact"):
                    for sector, impact in path_info["sector_impact"].items():
                        lines.append(f"- **{sector}**: {impact}")
            lines.append("")
            lines.append("以上仅供参考，你必须基于完整新闻内容和当前市场状况做出独立判断。")

        lines.append("")
        return "\n".join(lines)

    def get_path_impact(self, path_name: str) -> Dict:
        """获取特定传导路径的详细影响信息"""
        return US_TRANSMISSION_PATHS.get(path_name, {})
