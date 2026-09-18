"""
美股 Symbol 规范化工具 (SSOT for US symbol canonicalization)

解决两类写入侧数据卫生问题:
  1. 双前缀:对已带 "US." 的符号再拼一次 f"US.{sym}" → "US.US.MU"
  2. 别名割裂:同一底层公司被记成不同 ticker → 数据打碎

统一在写入咽喉 (playbook 指令、voting 预测) 调用,保证落库符号 canonical。
"""
import re
from typing import Optional

# 类股 (class share) 代码: 字母代码 + "-" + 1~2 位类别字母 (BRK-B / BF-B / RDS-A)。
# 故意不匹配期权代码 (BRK-B260119C500000 后缀为数字) — 期权符号不得改写。
_CLASS_SHARE_RE = re.compile(r"^([A-Z]{1,6})-([A-Z]{1,2})$")

# 被本系统当作"同一标的"的别名归并。canonical 取 watchlist 既有用法。
# 注意:GOOGL(Alphabet A类) 与 GOOG(C类) 技术上是两支不同股票,
#       但本动量系统按单一标的追踪 Alphabet,watchlist 用 GOOG,故归并。
SYMBOL_ALIASES = {
    "GOOGL": "GOOG",
}


def normalize_symbol(sym: Optional[str]) -> Optional[str]:
    """规范化美股符号。

    - 去首尾空白、转大写
    - 折叠重复的 "US." 前缀 (US.US.MU -> US.MU, 支持多重)
    - 归并别名 (GOOGL -> GOOG)
    - 保留原有的"是否带 US. 前缀"状态 (不擅自增删前缀)

    None / 空串原样返回。
    """
    if not sym:
        return sym
    s = str(sym).strip().upper()
    if not s:
        return s
    # 折叠任意重数的 "US." 前缀: "US.US.MU" -> "US.MU"
    while s.startswith("US.US."):
        s = s[3:]  # 去掉一个 "US."
    # 拆前缀取裸符号,做别名归并后再拼回
    if s.startswith("US."):
        base = s[3:]
        base = SYMBOL_ALIASES.get(base, base)
        return f"US.{base}"
    return SYMBOL_ALIASES.get(s, s)


def to_dotted_class_share(ticker: Optional[str]) -> Optional[str]:
    """类股 ticker 由连字符写法转点号写法 (BRK-B → BRK.B); 非类股原样返回。

    各源接受的类股写法实测不同 (2026-09-18 活体探针):
      TwelveData  : BRK-B → HTTP 404            | BRK.B → 200 (3 bars)
      Futu OpenD  : US.BRK-B → ret=-1 未知股票   | US.BRK.B → ret=0 (快照/K线均通)
      Yahoo       : BRK-B → 200                 | BRK.B → Not Found
      Finnhub     : 连字符可用 (仅报价源)
    故只在 TwelveData / Futu 的出站符号构造处调用; Yahoo/Finnhub 保持连字符原样。
    期权代码 (后缀为数字) 与普通 ticker 一律不改写。
    """
    if not ticker:
        return ticker
    t = str(ticker).strip()
    m = _CLASS_SHARE_RE.match(t)
    return f"{m.group(1)}.{m.group(2)}" if m else t
