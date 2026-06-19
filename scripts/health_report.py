#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 盘前/盘后健康报告 (Task #21)

可独立运行，也可被 scheduler 调用。

Usage:
    python scripts/health_report.py --mode pre        # 盘前
    python scripts/health_report.py --mode post       # 盘后
    python scripts/health_report.py --mode pre --notify-feishu
    python scripts/health_report.py --mode post --json /tmp/h.json

盘前检查项:
  - OpenD 连通性 (127.0.0.1:11111)
  - 数据库连接 & 关键表存在
  - API key 有效性（环境变量 + 简单请求测试）
  - 昨日交易摘要
  - 当前配置关键值快照

盘后检查项:
  - 当日交易汇总（笔数 / 盈亏 / 胜率）
  - 未关闭持仓列表
  - 错误日志计数（grep ERROR/CRITICAL）
  - 下一交易日待执行事项
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 路径与环境 ──
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 加载 .env.us（可选）
try:
    from dotenv import load_dotenv  # type: ignore
    env_file = BASE_DIR / ".env.us"
    if env_file.exists():
        load_dotenv(env_file, override=False)
except Exception:
    pass

DB_PATH = BASE_DIR / "data" / "ftseek_us.db"
LOG_DIR = BASE_DIR / "logs"

FUTU_OPEND_HOST = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
FUTU_OPEND_PORT = int(os.getenv("FUTU_OPEND_PORT", "11111"))


# ============================================================
# 通用工具
# ============================================================
def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if ok else "FAIL",
        "detail": detail,
    }


def _warn(name: str, detail: str) -> Dict[str, Any]:
    return {"name": name, "status": "WARN", "detail": detail}


def _check_opend() -> Dict[str, Any]:
    try:
        with socket.create_connection((FUTU_OPEND_HOST, FUTU_OPEND_PORT), timeout=3):
            return _check("OpenD 连通性", True, f"{FUTU_OPEND_HOST}:{FUTU_OPEND_PORT}")
    except Exception as e:
        return _check("OpenD 连通性", False, f"{FUTU_OPEND_HOST}:{FUTU_OPEND_PORT} 不可达: {e}")


def _check_db() -> Tuple[Dict[str, Any], Optional[sqlite3.Connection]]:
    if not DB_PATH.exists():
        return _check("数据库文件", False, f"{DB_PATH} 不存在"), None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        required = {"trade_records", "open_positions", "signals"}
        missing = required - tables
        if missing:
            return _check("数据库连接", False, f"缺失关键表: {missing}"), conn
        return _check("数据库连接", True, f"{DB_PATH.name} 表数={len(tables)}"), conn
    except Exception as e:
        return _check("数据库连接", False, f"{e}"), None


def _check_api_keys() -> List[Dict[str, Any]]:
    """检查关键 API key 是否设置（不发起远程请求，避免速率消耗）。"""
    checks = []
    keys = {
        "FTSEEK_US_API_TOKEN": True,                   # 必须
        "FINNHUB_API_KEY": True,                        # 主行情
        "TWELVEDATA_API_KEY": False,                    # 主行情
        "ALPHA_VANTAGE_API_KEY": False,                 # 备份
        "OPENAI_API_KEY": False,                        # LLM
        "FEISHU_WEBHOOK_URL_US": False,                 # 通知
    }
    for k, required in keys.items():
        v = os.getenv(k, "").strip()
        if v:
            checks.append(_check(f"API key:{k}", True, f"len={len(v)}"))
        elif required:
            checks.append(_check(f"API key:{k}", False, "未设置（必需）"))
        else:
            checks.append(_warn(f"API key:{k}", "未设置（可选）"))
    return checks


def _ping_finnhub() -> Dict[str, Any]:
    """轻量 ping Finnhub（仅在 key 存在时执行）。"""
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        return _warn("Finnhub 探活", "未配置 key，跳过")
    try:
        import urllib.request
        url = f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={key}"
        req = urllib.request.Request(url, headers={"User-Agent": "FTSeek-US-HealthReport"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if "c" in data:
                return _check("Finnhub 探活", True, f"AAPL c={data.get('c')}")
            return _check("Finnhub 探活", False, f"响应异常: {str(data)[:120]}")
    except Exception as e:
        return _check("Finnhub 探活", False, f"{e}")


def _yesterday_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    today = date.today()
    yesterday = today - timedelta(days=1)
    try:
        rows = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(realized_pnl), 0) AS pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM trade_records
            WHERE trade_date = ? AND exit_time IS NOT NULL
            """,
            (yesterday.isoformat(),),
        ).fetchone()
        n = int(rows["n"] or 0)
        pnl = float(rows["pnl"] or 0.0)
        wins = int(rows["wins"] or 0)
        win_rate = (wins / n * 100.0) if n else 0.0
        return {
            "date": yesterday.isoformat(),
            "trades": n,
            "realized_pnl": round(pnl, 2),
            "win_rate_pct": round(win_rate, 1),
        }
    except Exception as e:
        return {"date": yesterday.isoformat(), "error": str(e)}


def _today_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    today = date.today()
    try:
        rows = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(realized_pnl), 0) AS pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses
            FROM trade_records
            WHERE trade_date = ?
            """,
            (today.isoformat(),),
        ).fetchone()
        n = int(rows["n"] or 0)
        pnl = float(rows["pnl"] or 0.0)
        wins = int(rows["wins"] or 0)
        losses = int(rows["losses"] or 0)
        win_rate = (wins / n * 100.0) if n else 0.0
        return {
            "date": today.isoformat(),
            "trades": n,
            "wins": wins,
            "losses": losses,
            "realized_pnl": round(pnl, 2),
            "win_rate_pct": round(win_rate, 1),
        }
    except Exception as e:
        return {"date": today.isoformat(), "error": str(e)}


def _open_positions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT symbol, direction, size, avg_entry_price, current_sl,
                   strategy_id, entry_time
            FROM open_positions
            WHERE COALESCE(size, 0) != 0
            ORDER BY entry_time DESC
            LIMIT 50
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def _config_snapshot() -> Dict[str, Any]:
    """关键配置快照（仅展示 SSOT 入口的几个核心字段）。"""
    snap: Dict[str, Any] = {
        "MODE": os.getenv("FTSEEK_US_MODE", "unknown"),
        "TRADING_ENABLED": os.getenv("TRADING_ENABLED", "false"),
        "ENV": os.getenv("FTSEEK_US_ENV", "unknown"),
        "OPEND_MANAGED": os.getenv("OPEND_MANAGED", "external"),
        "DATABASE_URL": os.getenv("DATABASE_URL", ""),
    }
    cfg = BASE_DIR / "config" / "config.yaml"
    if cfg.exists():
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            for key in ("market", "risk_per_trade", "max_concurrent_positions"):
                if key in data:
                    snap[f"yaml.{key}"] = data[key]
        except Exception as e:
            snap["yaml.error"] = str(e)
    return snap


def _count_log_errors(hours: int = 24) -> Dict[str, Any]:
    """简单统计最近 N 小时主日志的 ERROR/CRITICAL 行数。"""
    log_file = LOG_DIR / "ftseek_us.log"
    if not log_file.exists():
        return {"file": str(log_file), "exists": False}
    cutoff = datetime.now() - timedelta(hours=hours)
    err = 0
    crit = 0
    try:
        with log_file.open("r", encoding="utf-8", errors="ignore") as f:
            # 只读末尾 ~5MB 防止超大日志
            try:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 5 * 1024 * 1024))
                f.readline()  # 丢弃半行
            except Exception:
                f.seek(0)
            for line in f:
                if " ERROR " in line or "[ERROR]" in line:
                    err += 1
                elif " CRITICAL " in line or "[CRITICAL]" in line:
                    crit += 1
        return {
            "file": str(log_file),
            "window_hours": hours,
            "errors": err,
            "criticals": crit,
        }
    except Exception as e:
        return {"file": str(log_file), "error": str(e)}


def _next_trading_day_todos() -> List[str]:
    todos = [
        "盘前：执行 health_report.py --mode pre",
        "盘前：确认 OpenD 已启动 (/opt/ftseek/start_opend.sh)",
        "盘前：检查 .env.us 中 API key 有效性",
        "盘前：审查未关闭持仓的止损止盈位",
    ]
    return todos


# ============================================================
# 报告主流程
# ============================================================
def run_pre_market() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "mode": "pre_market",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "errors": [],
        "warnings": [],
    }

    # 1. OpenD
    c = _check_opend()
    report["checks"].append(c)
    if c["status"] == "FAIL":
        report["errors"].append(c["detail"])

    # 2. DB
    db_check, conn = _check_db()
    report["checks"].append(db_check)
    if db_check["status"] == "FAIL":
        report["errors"].append(db_check["detail"])

    # 3. API keys
    for ck in _check_api_keys():
        report["checks"].append(ck)
        if ck["status"] == "FAIL":
            report["errors"].append(f"{ck['name']}: {ck['detail']}")
        elif ck["status"] == "WARN":
            report["warnings"].append(f"{ck['name']}: {ck['detail']}")

    # 4. Finnhub 探活（轻量）
    report["checks"].append(_ping_finnhub())

    # 5. 昨日摘要
    if conn is not None:
        report["yesterday_summary"] = _yesterday_summary(conn)

    # 6. 配置快照
    report["config_snapshot"] = _config_snapshot()

    if conn is not None:
        conn.close()

    report["overall_status"] = "FAIL" if report["errors"] else "PASS"
    return report


def run_post_market() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "mode": "post_market",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "errors": [],
        "warnings": [],
    }

    db_check, conn = _check_db()
    report["checks"].append(db_check)
    if db_check["status"] == "FAIL":
        report["errors"].append(db_check["detail"])

    if conn is not None:
        report["today_summary"] = _today_summary(conn)
        positions = _open_positions(conn)
        report["open_positions"] = positions
        report["open_positions_count"] = len(
            [p for p in positions if "error" not in p]
        )
        conn.close()

    report["log_errors"] = _count_log_errors(hours=24)
    report["next_trading_day_todos"] = _next_trading_day_todos()

    log = report.get("log_errors") or {}
    if log.get("criticals", 0) > 0:
        report["errors"].append(f"日志 CRITICAL 计数={log.get('criticals')}")
    if log.get("errors", 0) > 50:
        report["warnings"].append(f"日志 ERROR 计数过高={log.get('errors')}")

    report["overall_status"] = "FAIL" if report["errors"] else "PASS"
    return report


# ============================================================
# 输出
# ============================================================
def render_terminal(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    mode = report.get("mode", "?")
    status = report.get("overall_status", "?")
    icon = "✅" if status == "PASS" else "❌"
    lines.append("=" * 60)
    lines.append(f"  FTSeek-US 健康报告 [{mode}]  {icon} {status}")
    lines.append(f"  生成时间: {report.get('timestamp')}")
    lines.append("=" * 60)

    # 检查项
    lines.append("\n[ 检查项 ]")
    for c in report.get("checks", []):
        s = c["status"]
        sym = "✅" if s == "PASS" else ("⚠️ " if s == "WARN" else "❌")
        lines.append(f"  {sym} {c['name']:<24} {c.get('detail','')}")

    # 摘要
    if "yesterday_summary" in report:
        s = report["yesterday_summary"]
        lines.append("\n[ 昨日交易摘要 ]")
        lines.append(f"  date={s.get('date')}  trades={s.get('trades')}  "
                     f"pnl={s.get('realized_pnl')}  win_rate={s.get('win_rate_pct')}%")

    if "today_summary" in report:
        s = report["today_summary"]
        lines.append("\n[ 当日交易汇总 ]")
        lines.append(
            f"  date={s.get('date')}  trades={s.get('trades')}  "
            f"wins={s.get('wins')}  losses={s.get('losses')}  "
            f"pnl={s.get('realized_pnl')}  win_rate={s.get('win_rate_pct')}%"
        )

    if "open_positions" in report:
        positions = report["open_positions"]
        lines.append(f"\n[ 未关闭持仓 ({report.get('open_positions_count', 0)}) ]")
        for p in positions[:20]:
            if "error" in p:
                lines.append(f"  ❌ {p['error']}")
            else:
                lines.append(
                    f"  • {p.get('symbol'):<12} {p.get('direction'):<6} "
                    f"size={p.get('size')}  entry={p.get('avg_entry_price')}  "
                    f"sl={p.get('current_sl')}  strat={p.get('strategy_id')}"
                )

    if "log_errors" in report:
        le = report["log_errors"]
        lines.append("\n[ 日志错误统计 ]")
        if le.get("exists") is False:
            lines.append(f"  日志文件不存在: {le.get('file')}")
        elif "error" in le:
            lines.append(f"  读取异常: {le.get('error')}")
        else:
            lines.append(
                f"  窗口={le.get('window_hours')}h  ERROR={le.get('errors')}  "
                f"CRITICAL={le.get('criticals')}"
            )

    if "config_snapshot" in report:
        lines.append("\n[ 配置快照 ]")
        for k, v in report["config_snapshot"].items():
            lines.append(f"  {k:<20} = {v}")

    if "next_trading_day_todos" in report:
        lines.append("\n[ 下一交易日待执行 ]")
        for t in report["next_trading_day_todos"]:
            lines.append(f"  - {t}")

    if report.get("errors"):
        lines.append("\n[ 错误 ]")
        for e in report["errors"]:
            lines.append(f"  ❌ {e}")
    if report.get("warnings"):
        lines.append("\n[ 警告 ]")
        for w in report["warnings"]:
            lines.append(f"  ⚠️  {w}")

    lines.append("=" * 60)
    return "\n".join(lines)


def render_feishu(report: Dict[str, Any]) -> str:
    """飞书纯文本（机器人需含关键词 [FTSeek-US]）。"""
    mode = report.get("mode")
    status = report.get("overall_status")
    icon = "✅" if status == "PASS" else "❌"
    parts = [f"[FTSeek-US] {icon} 健康报告 [{mode}] {status}"]
    if "today_summary" in report:
        s = report["today_summary"]
        parts.append(
            f"今日: trades={s.get('trades')} pnl={s.get('realized_pnl')} "
            f"win_rate={s.get('win_rate_pct')}%"
        )
    if "yesterday_summary" in report:
        s = report["yesterday_summary"]
        parts.append(
            f"昨日: trades={s.get('trades')} pnl={s.get('realized_pnl')}"
        )
    if "open_positions_count" in report:
        parts.append(f"持仓: {report['open_positions_count']}")
    if "log_errors" in report:
        le = report["log_errors"]
        if isinstance(le, dict) and "errors" in le:
            parts.append(f"日志ERR={le.get('errors')} CRIT={le.get('criticals')}")
    if report.get("errors"):
        parts.append("错误: " + "; ".join(report["errors"][:3]))
    return "\n".join(parts)


def push_feishu(text: str) -> bool:
    try:
        from app.reports.feishu_push_service import get_feishu_push_service
        svc = get_feishu_push_service()
        if svc is None:
            print("[feishu] 服务未初始化（缺少 FEISHU_APP_ID/SECRET/CHAT_ID）")
            return False
        ok = svc.send_text(text)
        print(f"[feishu] 推送{'成功' if ok else '失败'}")
        return ok
    except Exception as e:
        print(f"[feishu] 推送异常: {e}")
        return False


# ============================================================
# CLI
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="FTSeek-US 盘前/盘后健康报告")
    parser.add_argument("--mode", choices=["pre", "post"], required=True,
                        help="pre=盘前 post=盘后")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="将完整报告写到 JSON 文件")
    parser.add_argument("--notify-feishu", action="store_true",
                        help="同步推送精简版到飞书")
    parser.add_argument("--quiet", action="store_true",
                        help="只输出 JSON，不打印人类可读")
    args = parser.parse_args()

    report = run_pre_market() if args.mode == "pre" else run_post_market()

    text = render_terminal(report)
    if not args.quiet:
        print(text)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.quiet:
            print(f"\n[json] 写入: {args.json_path}")

    if args.notify_feishu:
        push_feishu(render_feishu(report))

    return 0 if report.get("overall_status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
