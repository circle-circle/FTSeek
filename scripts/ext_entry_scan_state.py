#!/usr/bin/env python3
"""跨轮扫描状态缓存 (RFC-101 L2) —— 把"这一轮已算过、且输入按类别可证稳定"的**昂贵**结论跨进程复用。

为什么需要: 入场链的载体是 cron 每 5 分钟一个新进程 (`*/5 16-22`/`4-8`), 进程内的缓存与闭包
状态天然归零 ⇒ 同一批标的的昂贵判据 (逐票快照/价差深度/K线) 每轮重取。本模块只做一件事:
(symbol, ET日, 时段) + 拒绝原因类别 → 结论与下次复查时刻, 落盘给下一轮读。

三条硬纪律 (RFC-101 §3/§4):
  1) 只缓存昂贵类别 —— 廉价门 (价格/带/量) 从榜单行字段重算本身就是免费的, 缓存只会多加 I/O。
  2) 失效优先于命中 —— 每轮用新鲜榜单行比对 (价格/量/涨跌幅跳变) + `next_check_at` 到期,
     任一不满足即视为未命中 ⇒ 立刻重评。
  3) 故障一律 fail-open —— 读/写失败、结构损坏、开关关闭 ⇒ 返回 None / 空态, 让执行器走原
     路径。**宁多算一轮, 绝不少算一轮**。

回滚: `EXT_SCAN_STATE_CACHE=false` (默认) 且 `EXT_SCAN_STATE_SHADOW=false` ⇒ 本模块不被调用,
行为字节级不变。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "loops" / "ext_entry_scan" / "state.json"

# ── 失效阈值 (榜单行新鲜读数 vs 记录时读数) ────────────────────────────────────
PX_JUMP_PCT = 10.0      # 价格变动 >10% ⇒ 失效
VOL_JUMP_PCT = 50.0     # 成交量变动 >50% ⇒ 失效
CHG_JUMP_PP = 1.5       # 涨跌幅变动 >1.5 个百分点 ⇒ 失效

# ── 类别表: 拒绝原因 → (类别, 有效期秒, 省下的调用数估计) ──────────────────────
# 依据见 RFC-101 §3: fail_close=数据迟到是暂时态(短 TTL, 防把迟到钉成永久拒绝);
#   quote_quality=价差深度分钟级会变; structure=当日累计量价决定, 变化慢;
#   event_absent=日历会补标相位(实测空相位 11.8%), 不能整段钉死。
CLASS_RULES = {
    "fail_close": (600, 1),
    "quote_quality": (900, 2),
    "structure": (1800, 1),
    "event_absent": (1800, 1),
}
_REASON_CLASS = {
    # fail_close: 数据源暂时没有/迟到/出错 —— 绝不允许被长 TTL 钉死
    "surprise_stale": "fail_close", "src_err": "fail_close", "actual_missing": "fail_close",
    # quote_quality: 价差/深度/费用/漂移/流动性评级
    "spread": "quote_quality", "depth": "quote_quality", "fee": "quote_quality",
    "drift": "quote_quality", "grade": "quote_quality", "liquidity": "quote_quality",
    "entry_window": "quote_quality",
    # structure: 结构门 (VWAP/区间)
    "structure": "structure",
    # event_absent: 当日无同相位财报事件
    "calendar_missing": "event_absent", "no_same_phase_event": "event_absent",
    "earnings_event": "event_absent",
}
_CACHE = {"today": None, "state": None}     # 进程级: 一轮内 load 一次, 末尾 flush 一次
_STATS = {"hit": 0, "miss": 0, "expired": 0, "invalidated": 0, "saved": 0, "write_fail": 0,
          "shadow_hit": 0, "shadow_miss": 0}


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _env_flag("EXT_SCAN_STATE_CACHE")


def shadow_enabled() -> bool:
    return _env_flag("EXT_SCAN_STATE_SHADOW")


def active() -> bool:
    return enabled() or shadow_enabled()


def class_of(reason) -> str | None:
    """拒绝原因 → 类别 (不在表内 ⇒ None = 不缓存)。前缀匹配: 'spread_wide' → 'spread'。"""
    r = str(reason or "").strip().lower()
    if not r:
        return None
    if r in _REASON_CLASS:
        return _REASON_CLASS[r]
    for key, cls in _REASON_CLASS.items():
        if r.startswith(key):
            return cls
    return None


def reset_stats() -> None:
    for k in _STATS:
        _STATS[k] = 0


def stats() -> dict:
    return dict(_STATS)


def _now() -> float:
    return time.time()


def load(today: str) -> dict:
    """读状态; 任何异常/格式不符 ⇒ 空态 (fail-open)。跨 ET 日的旧条目在读取时即丢弃。"""
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {"et_date": today, "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"et_date": today, "entries": {}}
    if str(data.get("et_date") or "") != str(today):
        return {"et_date": today, "entries": {}}      # 跨日: 全部失效 (C2 的粗粒度失效)
    return data


def save(state: dict) -> bool:
    """原子写盘 (`app/utils/atomic_write.atomic_write_json`)。失败只记数, 不抛 (fail-open)。"""
    try:
        from app.utils.atomic_write import atomic_write_json
        atomic_write_json(str(STATE_PATH), state)
        return True
    except Exception:
        _STATS["write_fail"] += 1
        return False


def _state(today: str) -> dict:
    """进程级状态访问器: 一轮内只读盘一次 (载入时已按 ET 日失效)。"""
    if _CACHE.get("today") != today or not isinstance(_CACHE.get("state"), dict):
        _CACHE["today"], _CACHE["state"] = today, load(today)
    return _CACHE["state"]


def flush(today: str) -> bool:
    """一轮结束落盘 (先清到期条目)。未开启 ⇒ 不写盘。"""
    if not active():
        return False
    st = _state(today)
    purge_expired(st)
    return save(st)


def reset_cache() -> None:
    """仅测试用: 清进程级状态。"""
    _CACHE["today"], _CACHE["state"] = None, None


def _key(symbol: str, session: str) -> str:
    return f"{str(symbol).upper()}|{session}"


def _jumped(prev: dict, row: dict) -> str | None:
    """新鲜榜单行 vs 记录时读数 —— 返回失效原因, None = 未跳变。"""
    try:
        p_px, n_px = float(prev.get("px") or 0), float(row.get("px") or 0)
        if p_px > 0 and n_px > 0 and abs(n_px - p_px) / p_px * 100.0 > PX_JUMP_PCT:
            return "px_jump"
        p_vol, n_vol = float(prev.get("vol") or 0), float(row.get("vol") or 0)
        if p_vol > 0 and n_vol >= 0 and abs(n_vol - p_vol) / p_vol * 100.0 > VOL_JUMP_PCT:
            return "vol_jump"
        p_chg, n_chg = float(prev.get("chg") or 0), float(row.get("chg") or 0)
        if abs(n_chg - p_chg) > CHG_JUMP_PP:
            return "chg_jump"
    except Exception:
        return "row_unparsable"          # 读数不可比 ⇒ 保守判失效
    return None


def lookup(symbol: str, session: str, today: str, row: dict, state: dict | None = None) -> dict | None:
    """命中 ⇒ {"reason","class","cached_at"}；未命中/失效/关闭 ⇒ None。同时更新计数。"""
    if not active():
        return None
    st = state if state is not None else _state(today)
    ent = (st.get("entries") or {}).get(_key(symbol, session))
    if not isinstance(ent, dict):
        _STATS["miss"] += 1
        return None
    if _now() >= float(ent.get("next_check_at") or 0):
        _STATS["expired"] += 1
        return None
    why = _jumped(ent.get("row") or {}, row or {})
    if why:
        _STATS["invalidated"] += 1
        return None
    if enabled():
        _STATS["hit"] += 1
        _STATS["saved"] += int(CLASS_RULES.get(str(ent.get("class")), (0, 1))[1])
    else:
        _STATS["shadow_hit"] += 1        # 影子: 只记账, 不改变判定
    return {"reason": ent.get("reason"), "class": ent.get("class"),
            "gate_key": ent.get("gate_key"), "cached_at": ent.get("cached_at"),
            "shadow": not enabled()}


def record(symbol: str, session: str, today: str, reason, row: dict,
           state: dict | None = None, gate_key: str | None = None) -> bool:
    """记一条结论 (仅当类别可缓存且开关开启)。返回是否写入。"""
    if not active():
        return False
    cls = class_of(reason)
    if cls is None:
        return False
    st = state if state is not None else _state(today)
    ttl = int(CLASS_RULES.get(cls, (0, 1))[0])
    if ttl <= 0:
        return False
    st.setdefault("entries", {})[_key(symbol, session)] = {
        "reason": str(reason), "class": cls, "gate_key": gate_key or str(reason),
        "cached_at": int(_now()),
        "next_check_at": int(_now() + ttl),
        "row": {"px": row.get("px"), "vol": row.get("vol"), "chg": row.get("chg")},
    }
    return True


def purge_expired(state: dict) -> int:
    """清掉已到期条目 (避免文件无限增长)。返回清理条数。"""
    ents = state.get("entries") or {}
    dead = [k for k, v in ents.items()
            if not isinstance(v, dict) or _now() >= float(v.get("next_check_at") or 0)]
    for k in dead:
        ents.pop(k, None)
    return len(dead)


def observe_line(session: str, state: dict) -> str:
    """独立观测行 (契约: 不动闸计数主行)。"""
    s = _STATS
    n = len(state.get("entries") or {})
    mode = "on" if enabled() else ("shadow" if shadow_enabled() else "off")
    return (f"[ext_entry] 扫描状态[{session}] mode={mode} 条目={n} 命中={s['hit']} "
            f"影子命中={s['shadow_hit']} 到期={s['expired']} 失效={s['invalidated']} "
            f"省调用≈{s['saved']} 写失败={s['write_fail']}")