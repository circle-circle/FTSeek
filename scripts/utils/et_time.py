"""回测/审计脚本的时间尺子公共层 (RFC-070, 2026-08-30).

SSOT = app.utils.timezone (2026-08-23 R-8c 已修, DST 正确)。
本模块只做一件事: 给 scripts/ 下的 ad-hoc 脚本提供委托入口,
**禁止任何脚本再手写 `- timedelta(hours=12)`**。

背景: 6 个脚本各自手写 CST→ET 的 -12h 拷贝, 其中一份注释明写
"样本期 5-8 月全为 EDT" —— CST(UTC+8)→EDT(UTC-4) 差 12h,
但 →EST(UTC-5) 差 13h, 11 月冬令时后这是定时炸弹。
(R-8c 已在 app 侧修过同款; 本层把 scripts 侧的拷贝收编到同一把尺子。)

用法 (脚本以 `python scripts/xxx.py` 运行时 scripts/ 自动在 sys.path):
    from utils.et_time import naive_cst_to_et, parse_et_date
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from app.utils.timezone import ET, CST, _localize  # noqa: E402  (SSOT)

__all__ = ["naive_cst_to_et", "parse_et_date", "ET", "CST"]


def naive_cst_to_et(dt: datetime, keep_aware: bool = False) -> datetime:
    """naive 时间戳(视为 CST/UTC+8 服务器时间) → ET 墙钟, DST 正确。

    夏令时 (EDT) 结果与旧 `-12h` 完全一致; 冬令时 (EST) 正确 -13h。
    keep_aware=False → naive ET 墙钟 (兼容旧算术消费端, 如 .replace(tzinfo=utc));
    keep_aware=True  → aware ET (供与 aware bar 时间比较)。

    输入必须是无时区的 naive; aware 输入请走 app.utils.timezone.to_et。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        raise ValueError(
            "naive_cst_to_et 只接受 naive (视为 CST); aware 输入请用 app.utils.timezone.to_et"
        )
    aware_et = _localize(dt, CST).astimezone(ET)
    return aware_et if keep_aware else aware_et.replace(tzinfo=None)


def parse_et_date(ts) -> str:
    """三格式混杂时间戳 → ET 交易日 (YYYY-MM-DD)。

    a) 带偏移 (如 '-04:00'/'+08:00'/'+00:00') → 按偏移正确换算到 ET 取日期
       (不再用字符串前 10 位切片 —— 对非 -04:00 偏移在日期边界会错);
    b) 无偏移裸时间戳 (旧数据) → 视为 CST, 换算 ET 取日期 (DST 正确)。

    与 overnight_gap_carry_audit.et_date 旧实现的区别仅在冬令时正确性;
    当前库内数据 (全部 EDT 月份) 两种实现逐行等值 (2026-08-30 全量验证)。
    """
    ts = str(ts)
    dt = datetime.fromisoformat(ts[:19])
    if len(ts) > 10 and ts[-6] in "+-" and ts[-3] == ":":
        # aware: 手动附偏移 (fromisoformat 对 '-04:00' 已可解析, 此处显式保底)
        from datetime import timezone as _tz, timedelta as _td
        off = ts[-6:]
        h, m = int(off[1:3]), int(off[4:6])
        delta = _td(hours=h, minutes=m)
        if off[0] == "-":
            delta = -delta
        dt = dt.replace(tzinfo=_tz(delta))
        return dt.astimezone(ET).strftime("%Y-%m-%d")
    return naive_cst_to_et(dt).strftime("%Y-%m-%d")
