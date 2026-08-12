#!/usr/bin/env python3.11
"""美国宏观日历影子采集 (financial-data-query 数据源接入, record-only).

背景 (2026-08-12): 蚂蚁金融数据 API (financial-data-query skill) 评估结论 —
  该源对美股行情/K线/资金流无优势 (LV3/Finnhub 已覆盖), 唯一独家缺口是
  宏观指标层: 系统的 regime 识别目前只吃 SPY 价格行为 + VIX, 对"今天是否
  宏观数据披露日"零感知. 本脚本盘前拉取 8 个美国核心宏观指标的最新值/
  披露日, 落库供后续接 DA prompt regime context (本阶段纯采集, 不进决策).

设计:
  - 指标编码集已于 2026-08-12 经 meta-search 召回 + data-query 实弹验证锁定
    (8/8 返回真实数据). 编码是服务端稳定 ID, 固化在下方注册表, 不再走召回层.
  - 每日最多 1 次 (ET 日期幂等), 窗口 06:00-09:29 ET (PRE_MARKET 阶段).
  - 单次批量 data-query 拉全部 8 指标近 200 天序列 → 取最近 2 期算趋势,
    disclosure_date 近 3 日内 → recently_released 标记 (日历信号本体).
  - 失败降级: 重试 2 次仍失败 → WARNING 日志, 不写状态文件 (下轮自动重试),
    绝不触碰主流程. record-only, 无下游消费者, 静默失败仅损失当日快照.

输出:
  data/macro_calendar_history.jsonl  — 每次运行 1 行全量快照 (历史积累)
  data/macro_calendar_latest.json    — 最新快照 (原子写, 未来消费者读此文件)
  data/.macro_calendar_shadow_state.json — 幂等状态

挂载: scheduler_core._llm_strategy_loop PRE_MARKET 分支.
回滚: config/unified_config.yaml macro_calendar.shadow_collect_enabled=false + 重启.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek.macro_calendar_shadow")

_BASE = Path(__file__).resolve().parents[1]
_HISTORY_OUT = _BASE / "data" / "macro_calendar_history.jsonl"
_LATEST_OUT = _BASE / "data" / "macro_calendar_latest.json"
_STATE = _BASE / "data" / ".macro_calendar_shadow_state.json"

# ─── API 端点 (自包含客户端, 与 financial-data-query skill 协议 v1.5.3 对齐) ───
_API_BASE = "https://dfdatamcpnexus-prod.antgroup-inc.cn"
_API_VERSION = "1.5.3"
_QUERY_PATH = "/api/v1/common_query"
_TIMEOUT = 90
_MAX_ATTEMPTS = 2          # 首次 + 1 次重试
_RETRY_BACKOFF_SEC = 2.0
_LOOKBACK_DAYS = 200       # 拉取窗口: 月频指标足够取 6 期, 日频利率取近段
_RECENT_RELEASE_DAYS = 3   # disclosure_date 距今 ≤3 天 → recently_released

# ─── 指标注册表: 编码经 meta-search 召回 + data-query 实弹验证 (2026-08-12) ───
# (编码, 短名, 服务端指标全名, 频率)
INDICATORS = [
    ("500173772", "CPI同比", "美国:居民消费价格指数:当期同比增长率:月", "月"),
    ("500133721", "核心CPI同比(季调)", "美国:核心居民消费价格指数:季调:1982-1984年=100:当期同比增长率:月", "月"),
    ("500130235", "核心PCE同比", "美国:核心个人消费支出物价指数:当期同比增长率:月", "月"),
    ("500130130", "PPI同比", "美国:生产者价格指数:当期同比增长率:月", "月"),
    ("500000657", "非农新增就业(季调)", "美国:新增就业人数:季调:非农业部门:当期值:月", "月"),
    ("500174133", "失业率(季调)", "美国:失业率:季调:当期值:月", "月"),
    ("500128005", "联邦基金利率", "美国:利率:联邦基金:当期值:日", "日"),
    ("500130222", "制造业PMI", "美国:制造业采购经理指数:当期值:月", "月"),
]


def _enabled() -> bool:
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(
            "macro_calendar.shadow_collect_enabled", True))
    except Exception:
        return True


def _now_et() -> datetime:
    from app.utils.timezone import now_et
    return now_et()


def _api_key() -> str:
    return os.environ.get("FINANCIAL_DATA_API_KEY", "").strip()


def _post_common_query(mode: str, requests_list: List[Dict]) -> Dict:
    """POST /api/v1/common_query, 仅对超时/网络/5xx 重试."""
    import requests as _requests

    key = _api_key()
    if not key:
        raise RuntimeError("FINANCIAL_DATA_API_KEY 未设置 (应经 .env.us 注入)")

    headers = {
        "X-API-Key": key,
        "X-API-Version": _API_VERSION,
        "Content-Type": "application/json",
    }
    body = json.dumps({"mode": mode, "requests": requests_list},
                      ensure_ascii=False).encode("utf-8")

    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _requests.post(
                f"{_API_BASE}{_QUERY_PATH}", headers=headers, data=body,
                timeout=_TIMEOUT, allow_redirects=False, verify=True)
            if resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code} 服务端错误")
            elif not resp.ok:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                return resp.json()
        except (_requests.exceptions.Timeout,
                _requests.exceptions.ConnectionError) as e:
            last_err = e
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SEC)
    raise RuntimeError(f"宏观API调用失败 ({_MAX_ATTEMPTS}次): {last_err}")


def _fetch_series(now: datetime) -> Dict[str, List[Dict]]:
    """批量拉全部指标近 _LOOKBACK_DAYS 天序列, 按编码分组并按期排序."""
    start = (now - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    codes = [c for c, _, _, _ in INDICATORS]
    result = _post_common_query("data", [{
        "url": "/api/v1/macro/data-query",
        "params": {
            "indicator_codes": codes,
            "raw_question": "FTSeek-US 盘前宏观日历影子采集",
            "start_date": start,
        },
    }])

    if result.get("status") != "SUCCESS":
        issues = result.get("issues") or [{}]
        raise RuntimeError(f"宏观API返回FAILED: {issues[0].get('hint', issues)}")

    sub = (result.get("results") or [{}])[0]
    fields = (sub.get("meta") or {}).get("fields") or []
    try:
        i_code = fields.index("indicator_code")
        i_end = fields.index("end_date")
        i_val = fields.index("indicator_value")
        i_pub = fields.index("disclosure_date")
    except ValueError:
        raise RuntimeError(f"宏观API字段结构异常: fields={fields}")

    grouped: Dict[str, List[Dict]] = {c: [] for c in codes}
    for row in sub.get("data") or []:
        code = row[i_code]
        if code not in grouped:
            continue
        grouped[code].append({
            "end_date": str(row[i_end] or "")[:10],
            "value": row[i_val],
            "disclosure_date": str(row[i_pub] or "")[:10] if row[i_pub] else None,
        })
    for rows in grouped.values():
        rows.sort(key=lambda r: r["end_date"])
    return grouped


def _build_snapshot(now: datetime) -> Dict[str, Any]:
    grouped = _fetch_series(now)
    today = now.date()
    indicators = {}
    recent_releases = []

    for code, short_name, full_name, freq in INDICATORS:
        rows = grouped.get(code) or []
        if not rows:
            indicators[code] = {
                "name": short_name, "full_name": full_name, "frequency": freq,
                "status": "no_data",
            }
            logger.warning(f"[macro_shadow] {short_name}({code}) 窗口内无数据")
            continue

        latest = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None
        end_date = latest["end_date"]
        pub_date = latest["disclosure_date"]

        change = None
        if prev is not None and latest["value"] is not None \
                and prev["value"] is not None:
            change = round(float(latest["value"]) - float(prev["value"]), 6)

        days_since_release = None
        if pub_date:
            try:
                days_since_release = (
                    today - datetime.strptime(pub_date, "%Y-%m-%d").date()).days
            except ValueError:
                pass
        recently_released = (days_since_release is not None
                             and 0 <= days_since_release <= _RECENT_RELEASE_DAYS)

        entry = {
            "name": short_name,
            "full_name": full_name,
            "frequency": freq,
            "status": "ok",
            "period_end": end_date,
            "value": latest["value"],
            "prev_value": prev["value"] if prev else None,
            "change_vs_prev": change,
            "disclosure_date": pub_date,
            "days_since_release": days_since_release,
            "recently_released": recently_released,
            "n_periods": len(rows),
        }
        indicators[code] = entry
        if recently_released:
            recent_releases.append(
                f"{short_name}: {latest['value']} (期{end_date}, "
                f"披露{pub_date}, 前值{prev['value'] if prev else 'N/A'})")

    return {
        "run_ts_et": now.strftime("%Y-%m-%d %H:%M:%S"),
        "as_of_date": today.strftime("%Y-%m-%d"),
        "source": "ant-financial-data-api/macro (financial-data-query skill)",
        "indicators": indicators,
        "recent_releases": recent_releases,
    }


def _load_state() -> Dict:
    try:
        if _STATE.exists():
            return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: Dict) -> None:
    tmp = _STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STATE)


def _atomic_write_json(path: Path, obj: Dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def run_collect(scheduler=None, phase: str = "PRE_MARKET",
                force: bool = False) -> bool:
    """盘前宏观日历采集入口. 返回 True=本次实际执行了采集.

    scheduler 参数保持与其他影子采集脚本同构 (本脚本不依赖 scheduler 状态).
    """
    if phase != "PRE_MARKET":
        return False
    if not _enabled():
        logger.debug("[macro_shadow] 已禁用 (macro_calendar.shadow_collect_enabled=false)")
        return False

    now = _now_et()
    hhmm = now.hour * 100 + now.minute  # HHMM 编码, 两侧统一
    if not force and not (600 <= hhmm < 930):
        return False

    date_key = now.strftime("%Y-%m-%d")
    state = _load_state()
    if not force and state.get("last_run_date_et") == date_key:
        return False

    try:
        snapshot = _build_snapshot(now)
    except Exception as e:
        # 失败不写状态 → 下一轮循环自动重试; record-only 无主流程影响
        logger.warning(f"[macro_shadow] 采集失败 (下轮重试): {e}")
        return False

    _HISTORY_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    _atomic_write_json(_LATEST_OUT, snapshot)

    state["last_run_date_et"] = date_key
    state["last_run_ts"] = now.strftime("%Y-%m-%d %H:%M:%S")
    state["last_status"] = "ok"
    _save_state(state)

    n_ok = sum(1 for v in snapshot["indicators"].values()
               if v.get("status") == "ok")
    logger.info(
        f"[macro_shadow] 采集完成: {n_ok}/{len(INDICATORS)} 指标 | "
        f"近{_RECENT_RELEASE_DAYS}日新披露: "
        f"{'; '.join(snapshot['recent_releases']) or '无'}")
    return True


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="宏观日历影子采集 (手动验证用)")
    ap.add_argument("--force", action="store_true",
                    help="绕过时间窗口与每日幂等 (仅手动验证)")
    args = ap.parse_args()
    run_collect(None, phase="PRE_MARKET", force=args.force)
