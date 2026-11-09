#!/usr/bin/env python3.11
"""美国宏观日历影子采集 (record-only) — FRED 单源版 (2026-08-12 方案B).

背景: 蚂蚁金融数据API (financial-data-query skill) 评估后确认宏观指标是其唯一
  独家缺口, 但二次评估发现 FRED (美联储圣路易斯分行官方数据库) 公开 CSV 端点
  覆盖 7/8 指标且数据更新 (2026-08-12 实测: FRED 已有 2026-07 CPI, 蚂蚁 API
  停在 06), 无需 key 无限流. 用户裁决方案B: 全 FRED 单源, 砍 PMI (ISM 专有,
  FRED 无), 移除蚂蚁 API 依赖.

设计:
  - 数据源: https://fred.stlouisfed.org/graph/fredgraph.csv (公开, 无 key).
  - 7 个系列: CPI同比/核心CPI同比(季调)/核心PCE同比/PPI同比/非农新增/失业率/
    联邦基金利率(日频 DFF). 同比类用 transformation=pc1, 非农用 chg (月环比变化量).
  - 披露检测: FRED CSV 无披露日字段 → 用"最新期前移"判定: 本次 latest_period
    晚于上一次快照 (history 末行) = 期间内新数据披露. 月频系列参与披露判定,
    日频 DFF 仅跟踪水平值不算披露事件 (每天都有值, 无日历意义).
  - 每日最多 1 次 (ET 日期幂等), 窗口 06:00-09:29 ET (PRE_MARKET 阶段).
    FRED 更新通常在 ~08:00 ET, 盘前窗口正好捕获当日新披露.
  - 失败降级: 重试 2 次仍失败 → WARNING 日志, 不写状态文件 (下轮自动重试),
    绝不触碰主流程. record-only, 无下游消费者.

输出:
  data/macro_calendar_history.jsonl  — 每次运行 1 行全量快照 (历史积累,
                                       后续可从序列反推各指标实际披露日分布)
  data/macro_calendar_latest.json    — 最新快照 (原子写, 未来消费者读此文件)
  data/.macro_calendar_shadow_state.json — 幂等状态

挂载: scheduler_core._llm_strategy_loop PRE_MARKET 分支 (接口未变).
回滚: config/unified_config.yaml macro_calendar.shadow_collect_enabled=false + 重启.
"""
import csv
import io
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek.macro_calendar_shadow")

_BASE = Path(__file__).resolve().parents[1]
# 2026-08-21: CLI 直跑 (--force 手动验证) 需要 repo root 入 sys.path;
# 生产挂载走 scheduler 进程 sys.path 已含 root, 重复 insert 无害.
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))
_HISTORY_OUT = _BASE / "data" / "macro_calendar_history.jsonl"
_LATEST_OUT = _BASE / "data" / "macro_calendar_latest.json"
_STATE = _BASE / "data" / ".macro_calendar_shadow_state.json"

# ─── FRED 端点 (公开 CSV, 无 key 无限流) ────────────────────────────────────
_FRED_CSV_URL = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
                 "?id={series}&cosd={cosd}"
                 "{trans}")
_TIMEOUT = 30
_MAX_ATTEMPTS = 2          # 首次 + 1 次重试
_RETRY_BACKOFF_SEC = 2.0
_LOOKBACK_DAYS = 400       # ~13 个月, 月频系列足够取 12 期 + 同比窗口

# ─── 系列注册表: 2026-08-12 逐一实弹验证 (HTTP 200 + 数值与蚂蚁API交叉核对) ──
# (series_id, 短名, transformation, 频率, 参与披露判定)
SERIES = [
    ("CPIAUCSL", "CPI同比", "pc1", "月", True),
    ("CPILFESL", "核心CPI同比(季调)", "pc1", "月", True),
    ("PCEPILFE", "核心PCE同比", "pc1", "月", True),
    ("PPIACO", "PPI同比(全部商品)", "pc1", "月", True),
    ("PAYEMS", "非农新增就业(季调,千人)", "chg", "月", True),
    ("UNRATE", "失业率(季调)", "", "月", True),
    ("DFF", "联邦基金利率", "", "日", False),
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


def _make_session():
    """保留接口 (curl 实现无需 session), 为未来扩展预留."""
    return None


def _fetch_series_csv(session, series_id: str, cosd: str,
                      transformation: str) -> List[Tuple[str, float]]:
    """拉单个 FRED 系列 CSV → [(observation_date, value), ...] 升序.

    用 curl 而非 requests: 2026-08-12 实测 FRED WAF 会拦截 python-requests 的
    默认 UA (ReadTimeout 惩罚) 且拒绝自定义 UA 头 (HTTP 000), 但 curl 默认
    UA 稳定秒回 (7 系列连拉 ~3s). 必须保留 --http1.1 (HTTP/2 偶发 rc=92
    stream 错误). curl 缺失时抛错 → 上层降级.
    缺失值 FRED 记为 '.', 跳过. 非 CSV 响应 (限流/系列不存在) 由表头校验拦截.
    """
    import shutil
    import subprocess

    curl_bin = shutil.which("curl")
    if not curl_bin:
        raise RuntimeError("curl 不可用")

    trans = f"&transformation={transformation}" if transformation else ""
    url = _FRED_CSV_URL.format(series=series_id, cosd=cosd, trans=trans)

    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                [curl_bin, "-s", "--http1.1", "--max-time", str(_TIMEOUT), url],
                capture_output=True, text=True, timeout=_TIMEOUT + 5)
            if proc.returncode != 0:
                raise RuntimeError(f"curl rc={proc.returncode}")
            text = proc.stdout
            first_line = text.splitlines()[0] if text else ""
            if "observation_date" not in first_line:
                raise RuntimeError(f"响应非CSV (可能被限流/系列不存在): "
                                   f"{first_line[:80]}")
            rows: List[Tuple[str, float]] = []
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if not header or len(header) < 2:
                raise RuntimeError("CSV 缺少数值列")
            for rec in reader:
                if len(rec) < 2:
                    continue
                val_raw = rec[1].strip()
                if val_raw in ("", "."):
                    continue
                try:
                    rows.append((rec[0], float(val_raw)))
                except ValueError:
                    continue
            rows.sort(key=lambda r: r[0])
            return rows
        except (RuntimeError, subprocess.TimeoutExpired,
                subprocess.SubprocessError) as e:
            last_err = e
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SEC)
    raise RuntimeError(f"FRED {series_id} 拉取失败 ({_MAX_ATTEMPTS}次): {last_err}")


def _load_prev_snapshot() -> Dict[str, Dict]:
    """读 history 末行快照的 indicators 段 (披露检测基准). 无则空."""
    try:
        if _HISTORY_OUT.exists():
            last_line = None
            with open(_HISTORY_OUT, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            if last_line:
                return (json.loads(last_line).get("indicators") or {})
    except Exception as e:
        logger.warning(f"[macro_shadow] 读取历史快照失败 (按首日处理): {e}")
    return {}


def _fetch_futu_event_schedule(now: datetime) -> Dict[str, Any]:
    """兼容壳: 实现在 app/data/economic_calendar.py (2026-11-09 提升为生产模块).

    影子脚本保留此名称, 避免既有调用点/CLI 断裂; 逻辑单一真源在 app 侧。
    """
    from app.data.economic_calendar import fetch_us_event_schedule
    return fetch_us_event_schedule(now)


def _build_snapshot(now: datetime) -> Dict[str, Any]:
    cosd = (now - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    prev_indicators = _load_prev_snapshot()
    today = now.strftime("%Y-%m-%d")

    indicators: Dict[str, Dict[str, Any]] = {}
    new_releases: List[str] = []
    session = _make_session()

    for series_id, short_name, trans, freq, detect_release in SERIES:
        try:
            rows = _fetch_series_csv(session, series_id, cosd, trans)
        except Exception as e:
            indicators[series_id] = {
                "name": short_name, "frequency": freq, "status": "fetch_error",
                "error": str(e)[:200],
            }
            logger.warning(f"[macro_shadow] {short_name}({series_id}) 拉取失败: {e}")
            continue

        if not rows:
            indicators[series_id] = {
                "name": short_name, "frequency": freq, "status": "no_data",
            }
            logger.warning(f"[macro_shadow] {short_name}({series_id}) 窗口内无数据")
            continue

        latest_date, latest_val = rows[-1]
        prev_date, prev_val = (rows[-2] if len(rows) >= 2
                               else (None, None))
        change = (round(latest_val - prev_val, 6)
                  if prev_val is not None else None)

        # 披露检测: 月频系列最新期晚于上一次快照的最新期 = 新披露
        period_advanced = False
        prev_snap = prev_indicators.get(series_id) or {}
        prev_snap_period = prev_snap.get("period_end")
        if detect_release and prev_snap_period:
            period_advanced = latest_date > prev_snap_period

        indicators[series_id] = {
            "name": short_name,
            "frequency": freq,
            "status": "ok",
            "period_end": latest_date,
            "value": latest_val,
            "prev_value": prev_val,
            "change_vs_prev": change,
            "n_periods": len(rows),
            "period_advanced_since_last_run": period_advanced,
        }
        if period_advanced:
            new_releases.append(
                f"{short_name}: {latest_val} (期{latest_date}, "
                f"前值{prev_val if prev_val is not None else 'N/A'})")

    return {
        "run_ts_et": now.strftime("%Y-%m-%d %H:%M:%S"),
        "as_of_date": today,
        "source": "FRED (fred.stlouisfed.org, 公开CSV无key) + Futu经济日历(未来日程)",
        "indicators": indicators,
        "new_releases_this_run": new_releases,
        "has_prev_snapshot": bool(prev_indicators),
        # 2026-08-21: 未来7天美国HIGH/MEDIUM宏观事件日程 (fail-open, 不影响FRED段)
        "event_schedule": _fetch_futu_event_schedule(now),
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
        f"[macro_shadow] 采集完成: {n_ok}/{len(SERIES)} 指标 | "
        f"本次新披露: {'; '.join(snapshot['new_releases_this_run']) or '无'}")
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
