"""
美股 Symbol 规范化工具 (SSOT for US symbol canonicalization)

解决两类写入侧数据卫生问题:
  1. 双前缀:对已带 "US." 的符号再拼一次 f"US.{sym}" → "US.US.MU"
  2. 别名割裂:同一底层公司被记成不同 ticker → 数据打碎

统一在写入咽喉 (playbook 指令、voting 预测) 调用,保证落库符号 canonical。
"""
from typing import Optional

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
