"""盘中主动健康心跳 (2026-05-19)

第一性原则: 不为监控而监控 — 给"决策者"看的, 信息密度高 + 一眼看完.

设计目标:
- 默认 30min 一次; 生产由 `push.intraday_heartbeat_interval_min` 控制 (2026-10-18
  推送治理起 = 60min, 盘中约 7 次/天)。心跳是状态播报不是告警。
- 钉钉合并到现有 PushHub (利用 dedup/aggregate 基础设施)
- 7 个关键维度: 服务/持仓/资金/盈亏/信号/LLM/风控

可观测维度:
1. 🟢 服务健康: 运行时长 / broker 连接 / 线程心跳
2. 📊 持仓: 数量 / 主仓盈亏 / 占用资金
3. 💰 当日盈亏: 净 PnL / 已平笔数 / 胜率
4. 🎯 信号漏斗: 扫描数 / 入场数 / 拒绝原因 Top3
5. 🤖 LLM 链路: 调用数 / 成功率 / Pre-Gate 拦截数
6. ⚠️ 告警计数: ERROR / WARNING (按类聚合)
7. 🔵 风控状态: 熔断 / FLAT_ALL / capital 漂移

仅在盘中触发 (盘前/盘后跳过, 减噪音).
"""
from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.storage.db_pool import get_pool

logger = logging.getLogger("ftseek_us.intraday_heartbeat")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ─── 交易日判断: 委托到统一 SSOT (holiday_calendar) ───
# P1 修复 (2026-05-31): 删除本模块的 Futu API/缓存逻辑, 统一由 holiday_calendar 管理.
# 所有交易日判断 (含 Futu API + 硬编码降级) 集中在 app.utils.holiday_calendar.


def _is_trading_day(market: str = "US") -> bool:
    """检查今日是否为交易日.

    委托到 holiday_calendar SSOT (Futu API 优先 + 硬编码降级).
    使用 ET 日期, 与交易日语义一致.
    """
    from app.utils.timezone import get_trade_date
    from app.utils.holiday_calendar import get_holiday_calendar

    et_date_str = get_trade_date()
    et_date = datetime.strptime(et_date_str, "%Y-%m-%d").date()
    return get_holiday_calendar().is_trading_day(market, et_date)


def _is_market_hours() -> bool:
    """美股盘中 (09:30-16:00 ET).

    三重门控: 周末 → 假日/交易日 → 时间窗口.
    统一使用 app.utils.timezone 获取 ET 时间.
    """
    try:
        from app.utils.timezone import now_et

        et_now = now_et()

        # 1. 周末跳过
        if et_now.weekday() >= 5:
            return False

        # 2. 交易日检查 (委托到 SSOT)
        if not _is_trading_day("US"):
            return False

        # 3. 时间窗口: ET 09:30-16:00
        hour_min = et_now.hour + et_now.minute / 60.0
        return 9.5 <= hour_min <= 16.0
    except Exception:
        return True  # 安全侧: 失败时认为是盘中, 不抑制心跳


def _query_today_trades() -> Dict:
    """今日交易 + 持仓 + 盈亏汇总

    注意：美股交易日使用美东时间，而非北京时间。
    2026-07-24: "今日"统一走系统唯一真源 _et_trade_date(now_et()) (真 ET 时区,
    EDT/EST 自动) — 原 date('now','-4 hours') 近似在冬季 EST 20:00-24:00
    会把"今日"错成次日, 与 trade_date 写入标签口径分裂。
    """
    try:
        from app.utils.timezone import _et_trade_date, now_et
        conn = get_pool().connection("ftseek_us")
        cur = conn.cursor()
        # 2026-08-28 出场日记账 + 净额 (同 AccountCircuitBreaker 口径): 原
        # WHERE trade_date=? (入场日) + SUM(realized_pnl) (毛额) — 隔夜仓今日
        # 了结不计入且费用不可见 (8/27 实证: 该推送口径 10 笔/+381.15,
        # 出场日净额实为 13 笔/+119.37)。
        cur.execute(
            "SELECT COUNT(*), SUM(COALESCE(pnl_net, realized_pnl)), "
            "  SUM(CASE WHEN COALESCE(pnl_net, realized_pnl)>0 THEN 1 ELSE 0 END) "
            "FROM trade_records "
            "WHERE substr(exit_time, 1, 10)=? "
            "AND COALESCE(pnl_net, realized_pnl) IS NOT NULL "
            "AND exit_price > 0",
            (_et_trade_date(now_et()),),
        )
        n, pnl, wins = cur.fetchone()
        # 持仓
        cur.execute(
            "SELECT symbol, direction, size, avg_entry_price, current_sl, owner_strategy "
            "FROM open_positions WHERE size > 0"
        )
        positions = []
        for row in cur.fetchall():
            positions.append({
                "symbol": row[0], "direction": row[1], "size": row[2],
                "entry": row[3], "sl": row[4], "owner": row[5] or "?",
            })
        return {
            "today_trades": n or 0,
            "today_pnl": round(pnl or 0, 2),
            "today_wins": wins or 0,
            "positions": positions,
        }
    except Exception as e:
        logger.debug(f"[Heartbeat] trades query 失败: {e}")
        return {"today_trades": 0, "today_pnl": 0.0, "today_wins": 0, "positions": []}


def _query_signal_funnel(window_min: int = 30) -> Dict:
    """近 N 分钟信号漏斗: 入场数 + 拒绝数 + Top3 reason.

    入场数从 trade_records 查 (近 N min entry_time).
    拒绝数从 rejections.db 查 (近 N min timestamp).
    """
    from app.utils.timezone import now_et
    cutoff_iso = (now_et() - timedelta(minutes=window_min)).isoformat()
    entries = 0
    total_rejects = 0
    top_reasons: List[Tuple[str, int]] = []

    # 入场数 (trade_records 主 DB)
    try:
        conn = get_pool().connection("ftseek_us")
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM trade_records WHERE entry_time > ?",
            (cutoff_iso,)
        )
        entries = cur.fetchone()[0] or 0
    except Exception as e:
        logger.debug(f"[Heartbeat] entries query 失败: {e}")

    # 拒绝 (rejections.db)
    try:
        conn = get_pool().connection("rejections")
        cur = conn.cursor()
        cur.execute(
            "SELECT reason_category, COUNT(*) FROM rejections "
            "WHERE timestamp > ? GROUP BY reason_category ORDER BY COUNT(*) DESC LIMIT 3",
            (cutoff_iso,)
        )
        top_reasons = [(r[0], r[1]) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM rejections WHERE timestamp > ?", (cutoff_iso,))
        total_rejects = cur.fetchone()[0] or 0
    except Exception as e:
        logger.debug(f"[Heartbeat] rejects query 失败: {e}")

    return {
        "window_min": window_min,
        "entries": entries,
        "rejects": total_rejects,
        "top_reasons": top_reasons,
    }


def _query_llm_health(window_min: int = 30) -> Dict:
    """近 N 分钟 LLM 调用 / 成功率 / Pre-Gate 拦截.

    Pre-Gate 拦截统计精度: result_summary 的 JSON 解析, 而非脆弱 LIKE 匹配.
    """
    try:
        import json as _json
        conn = get_pool().connection("llm_audit")
        cutoff = time.time() - window_min * 60
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), SUM(success), "
            "  SUM(CASE WHEN feature='pre_entry_gate' THEN 1 ELSE 0 END) "
            "FROM llm_calls WHERE timestamp > ? AND COALESCE(is_test, 0) = 0", (cutoff,)
        )
        n, ok, pre_gate_n = cur.fetchone()
        # Pre-Gate 拦截数: 真正解析 response JSON, 而非 LIKE 字符串
        cur.execute(
            "SELECT result_summary FROM llm_calls "
            "WHERE feature='pre_entry_gate' AND timestamp > ? AND success=1 "
            "AND COALESCE(is_test, 0) = 0",
            (cutoff,)
        )
        pre_gate_blocked = 0
        for (summary,) in cur.fetchall():
            if not summary:
                continue
            try:
                obj = _json.loads(summary)
                # response 是一个 JSON 对象, 字段 "go": false 表示拒绝
                if isinstance(obj, dict) and obj.get("go") is False:
                    pre_gate_blocked += 1
            except Exception:
                # JSON 解析失败 → 不计 (避免误统计)
                pass
        success_pct = round(100.0 * (ok or 0) / max(1, n or 1), 1)
        return {
            "calls": n or 0,
            "success_pct": success_pct,
            "pre_gate_calls": pre_gate_n or 0,
            "pre_gate_blocked": pre_gate_blocked,
        }
    except Exception as e:
        logger.debug(f"[Heartbeat] LLM query 失败: {e}")
        return {"calls": 0, "success_pct": 0, "pre_gate_calls": 0, "pre_gate_blocked": 0}


def _query_alert_counts(window_min: int = 30) -> Dict:
    """近 N 分钟日志中的 ERROR/WARNING 数 (从 logs/ftseek_us.log 末尾扫描)"""
    log_path = PROJECT_ROOT / "logs" / "ftseek_us.log"
    if not log_path.exists():
        return {"errors": 0, "warnings": 0, "top_warning": ""}
    try:
        cutoff = (datetime.now() - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M")
        # 仅扫文件末尾 5MB (避免大文件全扫)
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 5_000_000))
            tail = f.read().decode("utf-8", errors="ignore")
        errors = 0
        warnings = 0
        warn_msgs: Dict[str, int] = {}
        for line in tail.split("\n"):
            if not line or len(line) < 30:
                continue
            ts = line[:16]
            if ts < cutoff:
                continue
            if "] ERROR:" in line:
                errors += 1
            elif "] WARNING:" in line:
                warnings += 1
                # 提取 warning 简短分类 (前 30 字)
                if "WARNING:" in line:
                    msg = line.split("WARNING:", 1)[1][:30].strip()
                    warn_msgs[msg] = warn_msgs.get(msg, 0) + 1
        top_warning = ""
        if warn_msgs:
            top_msg, top_n = max(warn_msgs.items(), key=lambda x: x[1])
            top_warning = f"{top_msg} (×{top_n})"
        return {"errors": errors, "warnings": warnings, "top_warning": top_warning}
    except Exception as e:
        logger.debug(f"[Heartbeat] alert query 失败: {e}")
        return {"errors": 0, "warnings": 0, "top_warning": ""}


def _query_risk_state(window_hours: int = 8) -> Dict:
    """风控状态: 近 N 小时熔断/FLAT_ALL 次数 (默认 8h ≈ 一个交易日窗口).

    避免"今日"语义混淆 — 美股盘中跨北京时间日界 (21:30 北京 → 04:00 北京次日),
    用滚动时间窗口比"日历日"更准确.
    """
    log_path = PROJECT_ROOT / "logs" / "ftseek_us.log"
    if not log_path.exists():
        return {"breakers": 0, "flat_all": 0, "window_hours": window_hours}
    cutoff = (datetime.now() - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M")
    breakers = 0
    flat_all = 0
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 10_000_000))  # 10MB 末尾
            tail = f.read().decode("utf-8", errors="ignore")
        for line in tail.split("\n"):
            if not line or len(line) < 16:
                continue
            ts = line[:16]
            if ts < cutoff:
                continue
            if "🚨 熔断触发" in line:
                breakers += 1
            if "FLAT_ALL" in line and "组合止损" in line:
                flat_all += 1
        return {"breakers": breakers, "flat_all": flat_all, "window_hours": window_hours}
    except Exception:
        return {"breakers": 0, "flat_all": 0, "window_hours": window_hours}


def _safe_query(fn, fallback):
    """单个 query 失败不应阻断整体心跳"""
    try:
        return fn()
    except Exception as e:
        name = getattr(fn, "__name__", "lambda")
        logger.debug(f"[Heartbeat·query] {name} 失败 (用 fallback): {e}")
        return fallback


# 服务启动时间戳 — 此模块加载时记一次. main.py import 触发设值.
# 兜底用, 优先 scheduler.start_time / psutil / /proc.
_SERVICE_START_TS: float = time.time()


def _get_uptime_sec(scheduler=None) -> Tuple[int, str]:
    """返回 (uptime_sec, source). 多级 fallback, source 标注真实数据来源."""
    import os
    # a. scheduler.start_time (业务最准)
    try:
        if scheduler and hasattr(scheduler, "start_time"):
            start = scheduler.start_time
            ts = start if isinstance(start, (int, float)) else (
                start.timestamp() if hasattr(start, "timestamp") else None
            )
            if ts and ts > 0:
                return int(time.time() - ts), "scheduler"
    except Exception:
        pass
    # b. psutil
    try:
        import psutil  # type: ignore
        return int(time.time() - psutil.Process(os.getpid()).create_time()), "psutil"
    except Exception:
        pass
    # c. /proc/self/stat (Linux)
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().split()
        with open("/proc/uptime") as f:
            sys_uptime = float(f.read().split()[0])
        clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        boot = time.time() - sys_uptime
        return int(time.time() - (boot + int(fields[21]) / clk)), "/proc"
    except Exception:
        pass
    # d. module load 兜底
    return int(time.time() - _SERVICE_START_TS), "module_load"


def _query_service_health(scheduler=None) -> Dict:
    """真实查询服务健康度: uptime + thread + broker."""
    import os
    uptime_sec, uptime_source = _get_uptime_sec(scheduler)
    result = {
        "uptime_sec": uptime_sec, "uptime_source": uptime_source,
        "pid": os.getpid(),
        "thread_ok": None, "broker_ok": None,
    }

    # 2. Thread health (若 scheduler.thread_manager 可用)
    try:
        if scheduler and hasattr(scheduler, "thread_manager"):
            health = scheduler.thread_manager.health_check()
            if isinstance(health, dict):
                # 期望 dict 形如 {"all_alive": bool, ...}
                result["thread_ok"] = bool(health.get("all_alive", health.get("ok", True)))
    except Exception as e:
        logger.debug(f"[Heartbeat] thread_manager.health_check 失败: {e}")

    # 3. Broker 连接 (轻量 ping, 不阻塞主流程)
    try:
        # position_sync 持有 broker 连接, 看 last_sync_time 是否在 60s 内
        from app.core.position_sync import get_us_position_sync
        sync = get_us_position_sync()
        last_sync = getattr(sync, "_last_sync_time", None)
        if last_sync:
            from app.utils.timezone import now_et
            age_sec = (now_et() - last_sync).total_seconds()
            result["broker_ok"] = (age_sec < 120)  # 2 分钟内 sync 算健康
            result["last_sync_age_sec"] = int(age_sec)
    except Exception as e:
        logger.debug(f"[Heartbeat] broker health 失败: {e}")

    return result


def _format_uptime(sec: int) -> str:
    """uptime 秒数 → 人类可读 (1h23m / 2d 5h)"""
    if sec < 0:
        return "未知"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}min"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60}min"
    return f"{sec // 86400}d{(sec % 86400) // 3600}h"


def _format_heartbeat(scheduler=None) -> str:
    """组装心跳报告 (Markdown 格式, 钉钉/飞书友好). 每个 query 独立容错."""
    from app.utils.timezone import now_et
    now = now_et()
    trades = _safe_query(_query_today_trades,
                          {"today_trades": 0, "today_pnl": 0.0, "today_wins": 0, "positions": []})
    funnel = _safe_query(lambda: _query_signal_funnel(30),
                          {"window_min": 30, "entries": 0, "rejects": 0, "top_reasons": []})
    llm = _safe_query(lambda: _query_llm_health(30),
                       {"calls": 0, "success_pct": 0, "pre_gate_calls": 0, "pre_gate_blocked": 0})
    alerts = _safe_query(lambda: _query_alert_counts(30),
                          {"errors": 0, "warnings": 0, "top_warning": ""})
    risk = _safe_query(lambda: _query_risk_state(window_hours=8),
                        {"breakers": 0, "flat_all": 0, "window_hours": 8})
    health = _safe_query(lambda: _query_service_health(scheduler),
                          {"uptime_sec": -1, "thread_ok": None, "broker_ok": None})

    # 服务字段 — 真实数据, 不再硬编码 "运行中".
    # uptime_source ∈ {scheduler, psutil, /proc, module_load} — 标注数据可信度.
    uptime_str = _format_uptime(health.get("uptime_sec", -1))
    uptime_src = health.get("uptime_source", "unknown")
    if uptime_src == "module_load":
        # module_load 不是真实进程 uptime, 显式标注
        uptime_str = f"{uptime_str} (估)"
    thread_str = ("✓" if health.get("thread_ok") is True else
                  "✗" if health.get("thread_ok") is False else "n/a")
    broker_str = ("✓" if health.get("broker_ok") is True else
                  "✗" if health.get("broker_ok") is False else "n/a")
    sync_age = health.get("last_sync_age_sec")
    sync_str = f" (sync {sync_age}s前)" if sync_age is not None else ""

    # 持仓汇总
    pos_lines = []
    for p in trades["positions"][:3]:  # 仅 Top3
        pnl_pct = 0.0
        # 简化: 不查实时价格 (需 broker 调用), 仅显示静态信息
        pos_lines.append(
            f"  ├ {p['symbol']} {p['direction']} {int(p['size'])}@${p['entry']:.2f} (SL ${p['sl']:.2f})"
        )
    pos_block = "\n".join(pos_lines) if pos_lines else "  ├ (无持仓)"

    # 拒因 Top3
    rej_block = ", ".join(
        f"{r}×{n}" for r, n in funnel["top_reasons"][:3]
    ) if funnel["top_reasons"] else "无"

    # 盈亏 emoji
    pnl_emoji = "🟢" if trades["today_pnl"] >= 0 else "🔴"
    win_rate_pct = round(100.0 * trades["today_wins"] / max(1, trades["today_trades"]), 1) \
        if trades["today_trades"] > 0 else 0

    risk_window = risk.get("window_hours", 8)
    lines = [
        f"💓 [FTSeek-US 盘中心跳] {now.strftime('%H:%M')} 北京",
        f"🟢 服务: 已运行 {uptime_str} | thread={thread_str} broker={broker_str}{sync_str}",
        f"📊 持仓 ({len(trades['positions'])} 只):",
        pos_block,
        f"{pnl_emoji} 已平盈亏(今日): ${trades['today_pnl']:+.2f} | "
        f"{trades['today_trades']} 笔 (胜率 {win_rate_pct}%)",
        f"🎯 信号 (近30min): 入 {funnel.get('entries', 0)} 笔 / 拒 {funnel['rejects']} 笔 [{rej_block}]",
        f"🤖 LLM (近30min): {llm['calls']} 调用 / 成功率 {llm['success_pct']}% | "
        f"Pre-Gate 拦 {llm['pre_gate_blocked']}/{llm['pre_gate_calls']}",
        f"⚠️ 告警 (近30min): {alerts['errors']} ERROR / {alerts['warnings']} WARN" +
        (f" | Top: {alerts['top_warning']}" if alerts['top_warning'] else ""),
        f"🔵 风控 (近{risk_window}h): 熔断 {risk['breakers']} 次 | FLAT_ALL {risk['flat_all']} 次",
    ]
    return "\n".join(lines)


def push_heartbeat(scheduler=None, force: bool = False) -> bool:
    """主入口 — 推一次心跳到钉钉/飞书.

    Args:
        scheduler: 可选, 用于读 uptime / 线程状态
        force: True 强制推 (跳过 market_hours 检查), 用于测试

    Returns:
        True 推送成功, False 已跳过/失败
    """
    if not force and not _is_market_hours():
        logger.debug("[Heartbeat] 非盘中, 跳过")
        return False

    text = _format_heartbeat(scheduler)
    try:
        from app.push import get_push_hub
        from app.push.message import PushMessage, Level, Category
        from app.utils.timezone import now_et
        _push_now = now_et()
        # NORMAL 级别 + dedup 30min (防 cron 重复触发)
        msg = PushMessage(
            title=f"FTSeek-US 心跳 {_push_now.strftime('%H:%M')}",
            body=text,
            level=Level.NORMAL,
            category=Category.SYSTEM,
            dedup_key=f"heartbeat_{_push_now.strftime('%Y%m%d_%H%M')[:11]}",  # 10min 粒度
        )
        ok = get_push_hub().push(msg)
        if ok:
            logger.info(f"[Heartbeat] ✅ 已推送 ({len(text)} chars)")
        else:
            logger.warning(f"[Heartbeat] 推送失败 (PushHub 全通道失败)")
        return ok
    except Exception as e:
        logger.warning(f"[Heartbeat] 推送异常: {e}")
        # 兜底: 直接打日志, 不让心跳本身失败影响主流程
        logger.info(f"[Heartbeat·fallback]\n{text}")
        return False


# ─────────────────────────────────────────────────────
# 后台线程 (供 scheduler 启动期注入)
# ─────────────────────────────────────────────────────
class HeartbeatLoop:
    """后台线程, 按 interval_min 触发 push_heartbeat (生产默认由 main.py 传 60)"""

    def __init__(self, interval_min: int = 30, scheduler=None):
        self.interval_sec = max(60, interval_min * 60)  # 至少 1min, 防误配
        self.scheduler = scheduler
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="HeartbeatLoop"
        )
        self._thread.start()
        logger.info(f"[Heartbeat] 后台线程已启动 (interval={self.interval_sec}s)")

    def stop(self):
        self._running = False

    def _loop(self):
        # 启动后立即推一次 (确认通道工作)
        try:
            push_heartbeat(self.scheduler, force=False)
        except Exception as e:
            logger.warning(f"[Heartbeat·init] {e}")

        while self._running:
            try:
                # 假日/周末: 跳过心跳推送 (市场关闭无需心跳)
                from app.core.scheduler_triggers import sleep_if_market_closed
                if sleep_if_market_closed(self.interval_sec, lambda: self._running):
                    continue

                time.sleep(self.interval_sec)
                if not self._running:
                    break
                push_heartbeat(self.scheduler, force=False)
            except Exception as e:
                logger.warning(f"[Heartbeat·loop] {e}")


# ─── CLI 入口 (供 cron / 手动测试) ───
if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    text = _format_heartbeat()
    print(text)
    if "--push" in sys.argv:
        push_heartbeat(force=force)
