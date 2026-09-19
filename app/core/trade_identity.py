"""Minimal trade identity shared by push and persistence."""
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


_PROJECT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_REAL_MODES = {"live", "real", "real_money"}
_SIM_MODES = {"sim", "simulate", "simulated", "paper"}
_PHASE_ALIASES = {
    "premarket": "premarket",
    "pre-market": "premarket",
    "pre_market": "premarket",
    "regular": "regular",
    "intraday": "regular",
    "normal": "regular",
    "afterhours": "afterhours",
    "after-hours": "afterhours",
    "after_hours": "afterhours",
}


_PHASE_CN = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后"}


def phase_cn(value: Any) -> str:
    """阶段中文名 — 全库唯一 SSOT (2026-09-18 用户裁「统一推送渠道 + 保持各阶段真实内容」)。

    `premarket/regular/afterhours` 三个规范相位 (见 _PHASE_ALIASES) 的展示名,
    供推送、日报、周报共用, 避免各处自行硬编码「盘前」而把盘后事件说成盘前
    (实测 9/18 US.WBD 18:35 ET 盘后建仓, 推送文案却是"盘前事件开仓")。

    入参可为原始 session 串 (含别名, 如 intraday/after-hours) 或规范相位;
    无法识别 → 返回空串 (**不臆造**阶段)。
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _PHASE_CN.get(_PHASE_ALIASES.get(raw, raw), "")


def phase_from_time(dt: Any = None) -> str:
    """按 ET 时钟判定交易阶段 → 规范相位 (premarket/regular/afterhours)。

    2026-09-18 (用户裁「保持各阶段真实内容」): 推送/统计需要阶段, 而多数 trade_records
    的 metadata 里**没有** session 字段 ⇒ 按成交时刻判定 (与延长时段链的
    SESSION_WINDOWS: 盘前 04:00–09:29 / 盘后 16:00–19:59 同源口径)。

    盘外时段 (20:00–03:59 ET) 返回空串 —— **不臆造**阶段 (隔夜单既不属盘前也不属盘后)。
    """
    try:
        import datetime as _dt
        d = dt
        if d is None:
            try:
                from app.utils.timezone import now_et as _now_et
                d = _now_et()
            except Exception:
                from zoneinfo import ZoneInfo
                d = _dt.datetime.now(ZoneInfo("America/New_York"))
        if isinstance(d, str):
            d = _dt.datetime.fromisoformat(d.replace("Z", "+00:00"))
        if not isinstance(d, _dt.datetime):
            return ""
        try:
            from app.utils.timezone import to_et as _to_et
            d = _to_et(d)
        except Exception:
            from zoneinfo import ZoneInfo
            if d.tzinfo is None:
                d = d.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            d = d.astimezone(ZoneInfo("America/New_York"))
        hm = d.hour * 60 + d.minute
        if 4 * 60 <= hm < 9 * 60 + 30:
            return "premarket"
        if 9 * 60 + 30 <= hm < 16 * 60:
            return "regular"
        if 16 * 60 <= hm < 20 * 60:
            return "afterhours"
        return ""
    except Exception:
        return ""


def is_test_symbol(symbol: Optional[str]) -> bool:
    """占位/测试标的 (单测 artifact、券商账户里的报废测试单)。

    ⚠️ 判据**只认** TEST / US.TEST / *.TEST —— 2026-09-18 曾试图扩展成"末段占位词" (把 US.FAKE 也纳入),
    但 US.FAKE 在既有测试里被当作**正常符号**使用 (见 tests/test_extended_hours_order_execution.py 等)
    ⇒ 扩展会误伤 8 个既有用例, 已回退。该推送问题的真正修法是: conftest 封死测试→生产通知出口 +
    _execute 的 REAL 分支改为环境级一次提示 (见 tests/conftest.py / scripts/extended_hours_entry_executor.py)。

    2026-09-18 二次扩展 (只补**测试惯用变体**, 实测漏网): 既有判据对 `US.TEST_D4` / `TEST_D4` / `US.TEST1`
    全判 False ⇒ 4 个影子统计文件里残留 11 行占位行 (`breakeven_shadow` 5 + `exit_param_shadow` 2 +
    `ah_exit_shadow` 1 + `eod_depth_gate_shadow` 1 + `shadow_technical_direction` 3(裸 TEST))。
    新增规则仅覆盖 `TEST_<后缀>` / `TEST<数字>` 这两种单测命名习惯; **仍不**纳入 US.FAKE / TESTAR / USTEST
    (前者被既有用例当正常符号, 后两者像真 ticker)。
    """
    normalized = str(symbol or "").strip().upper()
    if normalized in {"TEST", "US.TEST"} or normalized.endswith(".TEST"):
        return True
    for segment in normalized.split("."):
        if segment.startswith("TEST_") or (segment.startswith("TEST") and segment[4:].isdigit()):
            return True
    return False


def trade_identity(
    symbol: str,
    source_type: Optional[str] = None,
    trade_mode: Optional[str] = None,
    session: Optional[str] = None,
) -> Dict[str, str]:
    source = str(source_type or "").strip().upper()
    mode = str(trade_mode or "").strip().lower()
    if is_test_symbol(symbol):
        trade_env = "test"
    elif "LIVE" in source or mode in _REAL_MODES:
        trade_env = "real"
    elif "SIM" in source or mode in _SIM_MODES:
        trade_env = "sim"
    else:
        trade_env = "unknown"

    identity: Dict[str, str] = {"trade_env": trade_env}
    phase = _PHASE_ALIASES.get(str(session or "").strip().lower())
    if phase:
        identity["trade_phase"] = phase
    return identity


def summarize_trade_identity(rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups = {}
    for row in rows:
        metadata = row.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        identity = trade_identity(
            row.get("symbol"),
            row.get("source_type"),
            row.get("trade_mode") or row.get("mode") or row.get("account_type"),
            row.get("session")
            or row.get("exit_session")
            or row.get("entry_session")
            or metadata.get("session"),
        )
        key = (
            identity["trade_env"],
            str(row.get("source_type") or "UNKNOWN").upper(),
            identity.get("trade_phase", "unknown"),
        )
        group = groups.setdefault(
            key, {"trade_env": key[0], "source_type": key[1], "trade_phase": key[2],
                  "n": 0, "wins": 0, "pnl": 0.0}
        )
        pnl = row.get("pnl_net")
        if pnl is None:
            pnl = row.get("realized_pnl") or 0.0
        group["n"] += 1
        group["wins"] += int(float(pnl) > 0)
        group["pnl"] += float(pnl)
    for group in groups.values():
        group["pnl"] = round(group["pnl"], 2)
    return sorted(groups.values(), key=lambda item: (-item["n"], item["source_type"]))


def ensure_test_symbol_not_in_production_db(
    symbol: Optional[str], db_path: Any
) -> None:
    if not is_test_symbol(symbol):
        return
    try:
        resolved = Path(db_path).resolve()
        is_production_db = resolved == _PROJECT_DATA_DIR or _PROJECT_DATA_DIR in resolved.parents
    except (TypeError, OSError):
        return
    if is_production_db:
        raise ValueError(
            f"test symbol {symbol!r} cannot be written to production DB: {resolved}"
        )
