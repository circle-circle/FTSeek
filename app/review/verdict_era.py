"""裁决参数指纹 (era 适用性自动标注) — RFC-138 借鉴①, 2026-10-08 用户裁「同意」.

解决的问题 (三次同型事故, 此前全靠人工「era 分层纪律」补丁):
  · be_0.4 归因错误 — 旧裁决在 be_0.5 参数时代做出, 落地后被错误引用;
  · 回放引擎裁决后被改 (ef5a1cc5d) — 旧 DEB 数字无法复现;
  · entry_band_widen 判据串台 (DEB-20261007) — 判据文本漂移无人察觉。
共同根因: 裁决结论不带参数指纹 ⇒ 参数时代变了, 旧结论仍可被无标注复活。

设计 (全部复用既有零件, 零新数据源):
  · 指纹真源 = app/playbook/config.get_exit_config_version() (退出参数哈希, 7/20 起
    逐笔写入 trade_records.metadata_json.config_version);
  · era 历史从生产 DB **只读推导** (trade_date → config_version 多数派), 不给
    ADJUDICATED 41 条记录手工补指纹 — 手工登记 = 第二真源 = 新的漂移面;
  · fail-open: 任何异常 ⇒ applies=None + 注明失败 (呈现面尺子坏了不拦报告,
    也不冒充判定; 与推送门 fail-open 先例一致)。

边界 (诚实声明, 详见 RFC-138 §5):
  · 指纹覆盖面 = **退出参数** (exit_config_version 设计即只哈希退出侧键);
    入场/仓位参数漂移本模块测不到, 标注文案写明「退出参数时代」;
  · era 精度 = 交易日粒度 (同日多版本取多数派并标 ambiguous, 实测 57 天中 10 天);
  · 只标注不降级: 「era 不符 ⇒ 自动降判据状态」是治理动作, 须用户裁, 不在本模块。

消费方 (全部呈现面, 交易链路零接触):
  app/review/criteria_status.pending_adjudications() → pending 报告 / shadow_board /
  review_bundle; scripts/exit_shadow_graduation.py (era 块)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)

# 打标起点: metadata_json.config_version 自 2026-07-20 (get_exit_config_version 上线) 起写入。
# 早于此的 read_at 无法推导 era ⇒ 显式 unknown, 不臆造。
_TAGGING_EPOCH = "2026-07-20"
# era_of_date 回溯上限: read_at 落在长假/停采窗时向前找最近有数据日, 超过即 unknown
# (30 天 ≈ 最长连休 + 余量; 再远的映射已不代表"裁决当时的参数")。
_LOOKBACK_DAYS = 30


def _db_path() -> Path:
    """主库路径 (走 SSOT, 不硬编码; live/sim 路由由 db_utils 决定)。"""
    from app.core.db_utils import get_main_db
    return get_main_db()


def _current_fingerprint() -> str:
    """当前退出参数指纹 (与 trade_records 打标同源同函数 ⇒ 同快照可比)。"""
    from app.playbook.config import get_playbook_config
    return get_playbook_config().get_exit_config_version()


def era_map() -> Dict[str, Dict[str, Any]]:
    """trade_date → 该日生效退出参数版本 (多数派), 从生产 DB 只读推导。

    返回 {date: {"version": str|None, "ambiguous": bool, "n": int}}。
    同日多版本 (改版当日, 实测 10/57 天) 取笔数多数派并标 ambiguous=True —
    披露而非隐瞒; 全 NULL 的日子不入表 (pre-tagging 或当日无打标笔)。
    """
    out: Dict[str, Dict[str, Any]] = {}
    db = _db_path()
    if not db.exists():
        return out
    conn = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
    try:
        rows = conn.execute(
            "SELECT trade_date,"
            " json_extract(metadata_json,'$.config_version') cv, COUNT(*)"
            " FROM trade_records"
            " WHERE trade_date IS NOT NULL"
            " GROUP BY trade_date, cv ORDER BY trade_date"
        ).fetchall()
    finally:
        conn.close()
    agg: Dict[str, Dict[str, int]] = {}
    for day, cv, n in rows:
        day = str(day)[:10]
        if not cv:
            continue  # NULL = 未打标 (pre-7/20 或写方缺口), 不参与多数派
        agg.setdefault(day, {})[str(cv)] = int(n)
    for day, cvs in agg.items():
        ranked = sorted(cvs.items(), key=lambda kv: -kv[1])
        out[day] = {
            "version": ranked[0][0],
            "ambiguous": len(ranked) > 1,
            "n": sum(cvs.values()),
        }
    return out


def era_of_date(day: str, _map: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """某日生效的退出参数版本; 无数据日向前回溯 (≤30 天), 早于打标起点 → unknown。

    返回 {"version": str|None, "as_of": str, "ambiguous": bool, "unknown_reason": str}。
    version=None 时 unknown_reason 非空 — 显式 unknown 不冒充判定。
    """
    emap = _map if _map is not None else era_map()
    res = {"version": None, "as_of": "", "ambiguous": False, "unknown_reason": ""}
    if not day:
        res["unknown_reason"] = "read_at 为空"
        return res
    if day < _TAGGING_EPOCH:
        res["unknown_reason"] = "早于打标起点 {} (era 不可推导)".format(_TAGGING_EPOCH)
        return res
    try:
        cur = date.fromisoformat(day[:10])
    except ValueError:
        res["unknown_reason"] = "read_at 非法日期格式: {!r}".format(day)
        return res
    for _ in range(_LOOKBACK_DAYS + 1):
        key = cur.isoformat()
        hit = emap.get(key)
        if hit:
            res.update(version=hit["version"], as_of=key, ambiguous=hit["ambiguous"])
            return res
        cur -= timedelta(days=1)
        if cur.isoformat() < _TAGGING_EPOCH:
            break
    res["unknown_reason"] = "近 {} 天无打标交易日可回溯".format(_LOOKBACK_DAYS)
    return res


def era_status(read_at: str, _emap: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """裁决日指纹 vs 当前指纹 → 适用性三态 (借鉴 riskbound applies_to_current_config)。

    返回 {"then": str|None, "now": str, "applies": True/False/None, "note": str}:
      applies=True  — 同代, 裁决适用当前参数;
      applies=False — 参数时代已漂移, 结论适用性未验 (呈现面应标 ⚠️);
      applies=None  — 不可判定 (era unknown / 查询异常), fail-open 不冒充判定。
    _emap: 批量调用时传入已算好的 era_map() (防 N 条记录 N 次 DB 查询)。
    """
    out: Dict[str, Any] = {"then": None, "now": "", "applies": None, "note": ""}
    try:
        out["now"] = _current_fingerprint()
        then = era_of_date(read_at, _emap)
        out["then"] = then["version"]
        if then["version"] is None:
            out["note"] = "era 不可推导 ({}) — 不作适用性判定".format(then["unknown_reason"])
            return out
        if then["version"] == out["now"]:
            suffix = " (裁决日同日多版本, 取多数派)" if then["ambiguous"] else ""
            out["applies"] = True
            out["note"] = "裁决日退出参数 == 现役 ({}, as_of {}){}".format(
                then["version"], then["as_of"], suffix)
            return out
        out["applies"] = False
        out["note"] = ("参数时代漂移: 裁决于 {} (as_of {}), 现役 {} — "
                       "结论适用性未验, 引用前须重裁或复算".format(
                           then["version"], then["as_of"], out["now"]))
        return out
    except Exception as e:  # noqa: BLE001 — 呈现面尺子 fail-open: 不拦报告, 不冒充判定
        _log.warning("[verdict_era] era_status 查询失败 (fail-open 不作判定): %s", e,
                     exc_info=True)
        out["applies"] = None
        out["note"] = "era 查询失败 ({}: {}) — 不作适用性判定".format(type(e).__name__, e)
        return out
