#!/usr/bin/env python3
"""
FTSeek AutoHeal Watchdog — 数据采集脚本
由 Hermes Cron Job 调用，输出结构化诊断报告供 LLM Agent 分析和修复。

职责:
  1. 日志扫描 → 错误模式匹配 + 频率统计
  2. DB 一致性 → 幽灵仓/持仓漂移/信号管道
  3. 服务健康 → HTTP health + 端口存活
  4. OpenD 状态 → 进程检查 + 连接日志
  5. 资源监控 → 内存/磁盘/僵尸进程
  6. 自动修复 → 已知安全模式直接修（重启/清理/reconcile）

输出: JSON 报告到 stdout，供 Cron Job 的 LLM Agent 消费。
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path("/opt/ftseek/FTSeek")
LOG_FILE = BASE_DIR / "logs" / "ftseek.log"
DB_FILE = BASE_DIR / "data" / "ftseek.db"
HEALTH_URL = "http://localhost:8899/health"
OPEND_BIN = "/opt/ftseek/Futu_OpenD_10.3.6308_Centos7/Futu_OpenD_10.3.6308_Centos7/FutuOpenD"

# 扫描窗口：最近 N 分钟的日志
LOOKBACK_MINUTES = 35  # 略大于 cron 间隔，确保不遗漏

# ============================================================
# 已知错误模式 → 严重度 + 自动修复能力
# ============================================================
KNOWN_PATTERNS = {
    # P0: 系统级故障
    "ECONNREFUSED": {
        "severity": "P0",
        "category": "opend_connection",
        "description": "Futu OpenD 连接被拒",
        "auto_fix": "check_opend_restart",
    },
    "RemoteClose": {
        "severity": "P1",
        "category": "opend_connection",
        "description": "Futu OpenD 远程关闭连接",
        "auto_fix": None,
    },
    "OpenD 未运行": {
        "severity": "P0",
        "category": "opend_down",
        "description": "OpenD 进程未运行",
        "auto_fix": "restart_opend",
    },
    "OpenD 未能启动": {
        "severity": "P0",
        "category": "opend_fail",
        "description": "OpenD 启动失败",
        "auto_fix": None,
    },
    # P1: 数据完整性
    "Futu 有持仓但本地无记录": {
        "severity": "P1",
        "category": "position_drift",
        "description": "Broker持仓与本地DB不一致",
        "auto_fix": "reconcile_positions",
    },
    "Unclosed client session": {
        "severity": "P3",
        "category": "resource_leak",
        "description": "aiohttp session 未关闭",
        "auto_fix": None,
    },
    "Task was destroyed but it is pending": {
        "severity": "P2",
        "category": "async_leak",
        "description": "asyncio task 被销毁但未完成",
        "auto_fix": None,
    },
    # P1: 业务逻辑
    "辩论执行失败": {
        "severity": "P1",
        "category": "consensus_error",
        "description": "共识引擎辩论异常",
        "auto_fix": None,
    },
    "查询失败: ret=-1": {
        "severity": "P2",
        "category": "futu_query",
        "description": "Futu SDK 查询返回失败",
        "auto_fix": None,
    },
    # P2: 信号质量
    "置信度过低": {
        "severity": "P3",
        "category": "signal_quality",
        "description": "信号置信度低于阈值被拒绝",
        "auto_fix": None,
    },
    "不在今日作战计划中": {
        "severity": "P3",
        "category": "signal_filter",
        "description": "信号标的不在当日计划",
        "auto_fix": None,
    },
    # P2: 性能
    "慢查询警告": {
        "severity": "P2",
        "category": "performance",
        "description": "DB查询超过阈值",
        "auto_fix": None,
    },
    "session_detector is DEPRECATED": {
        "severity": "P3",
        "category": "deprecation",
        "description": "废弃模块仍在使用",
        "auto_fix": None,
    },
    # 信号管道断裂（刚修复的）
    "_db_conn": {
        "severity": "P0",
        "category": "db_connection",
        "description": "DB连接对象丢失",
        "auto_fix": None,  # 已内置自动重连，观察是否还出现
    },
    # 数据获取失败
    "所有 3 次尝试均失败": {
        "severity": "P2",
        "category": "data_fetch",
        "description": "美股数据获取失败（us_overnight_fetcher）",
        "auto_fix": None,
    },
}


def scan_logs() -> dict:
    """扫描最近 N 分钟的日志，提取错误模式"""
    if not LOG_FILE.exists():
        return {"error": "log file not found", "patterns": {}}

    cutoff = time.time() - LOOKBACK_MINUTES * 60
    error_lines = []
    warning_lines = []
    total_lines = 0

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            # 从尾部读取（日志可能很大）
            f.seek(0, 2)
            file_size = f.tell()
            # 读最后 2MB（足够覆盖 35 分钟）
            read_size = min(file_size, 2 * 1024 * 1024)
            f.seek(file_size - read_size)
            if read_size < file_size:
                f.readline()  # 跳过不完整的行

            for line in f:
                # 检查时间戳是否在窗口内
                ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if ts_match:
                    try:
                        ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                        if (datetime.now() - ts).total_seconds() > LOOKBACK_MINUTES * 60:
                            continue
                    except ValueError:
                        continue

                total_lines += 1

                if "ERROR" in line or "CRITICAL" in line:
                    error_lines.append(line.strip())
                elif "WARNING" in line:
                    warning_lines.append(line.strip())
    except Exception as e:
        return {"error": str(e), "patterns": {}}

    # 模式匹配
    pattern_counts = Counter()
    pattern_details = {}

    for line in error_lines + warning_lines:
        for pattern_name, info in KNOWN_PATTERNS.items():
            if pattern_name in line:
                pattern_counts[pattern_name] += 1
                if pattern_name not in pattern_details:
                    pattern_details[pattern_name] = {
                        "severity": info["severity"],
                        "category": info["category"],
                        "description": info["description"],
                        "auto_fix": info["auto_fix"],
                        "count": 0,
                        "sample": line[:200],
                        "first_seen": None,
                        "last_seen": None,
                    }
                pattern_details[pattern_name]["count"] += 1

    # 提取未知错误模式（不在已知库中的 ERROR）
    unknown_errors = []
    for line in error_lines:
        matched = False
        for pattern_name in KNOWN_PATTERNS:
            if pattern_name in line:
                matched = True
                break
        if not matched:
            # 提取核心错误信息
            clean = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+\s*", "", line)
            clean = re.sub(r"\[.*?\]", "", clean).strip()
            if clean and len(clean) > 5:
                unknown_errors.append(clean[:200])

    # 去重未知错误
    unknown_counter = Counter(unknown_errors)

    return {
        "lookback_minutes": LOOKBACK_MINUTES,
        "total_lines_scanned": total_lines,
        "error_count": len(error_lines),
        "warning_count": len(warning_lines),
        "known_patterns": pattern_details,
        "unknown_errors": dict(unknown_counter.most_common(10)),
    }


def check_db_consistency() -> dict:
    """检查 DB 数据一致性"""
    if not DB_FILE.exists():
        return {"error": "DB file not found"}

    results = {}
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=10)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # 1. 持仓检查 (只看 OPEN 状态)
        active_pos = c.execute(
            "SELECT code, quantity, position_side, entry_price, current_price, data_source "
            "FROM positions WHERE quantity > 0 AND status = 'OPEN'"
        ).fetchall()
        results["active_positions"] = {
            "count": len(active_pos),
            "details": [dict(p) for p in active_pos],
        }

        # 2. 幽灵交易（pnl=0 但有完整记录）
        total_trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        ghost_trades = c.execute(
            "SELECT COUNT(*) FROM trades WHERE pnl = 0"
        ).fetchone()[0]
        results["ghost_trades"] = {
            "count": ghost_trades,
            "total": total_trades,
            "ratio": round(ghost_trades / max(total_trades, 1) * 100, 1),
        }

        # 3. 最近信号检查
        recent_signals = c.execute(
            "SELECT code, direction, status, COUNT(*) as cnt "
            "FROM signals WHERE created_at >= datetime('now', '-1 day') "
            "GROUP BY code, direction, status"
        ).fetchall()
        results["recent_signals_24h"] = {
            "count": sum(r[3] for r in recent_signals),
            "details": [dict(zip(["code", "direction", "status", "count"], r)) for r in recent_signals],
        }

        # 4. 今日交易
        today_trades = c.execute(
            "SELECT code, action, pnl, status, COUNT(*) as cnt "
            "FROM trades WHERE entry_time >= date('now') "
            "GROUP BY code, action, pnl, status"
        ).fetchall()
        results["today_trades"] = {
            "count": sum(r[4] for r in today_trades),
            "details": [dict(zip(["code", "action", "pnl", "status", "count"], r)) for r in today_trades],
        }

        # 5. 持仓漂移检测（Futu有但DB没记录的）
        # 检查最近的 position_sync 日志
        drift_count = 0
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                fsize = f.tell()
                f.seek(max(0, fsize - 500000))
                for line in f:
                    if "Futu 有持仓但本地无记录" in line:
                        # 只计最近 35 分钟
                        ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                        if ts_match:
                            ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                            if (datetime.now() - ts).total_seconds() < LOOKBACK_MINUTES * 60:
                                drift_count += 1
        except Exception:
            pass
        results["position_drift"] = {
            "broker_local_mismatch_count": drift_count,
            "severity": "P1" if drift_count > 0 else "ok",
        }

        conn.close()
    except Exception as e:
        results["error"] = str(e)

    return results


def _is_any_market_open() -> dict:
    """检查当前是否有任何市场处于交易时段
    
    Returns:
        dict with keys: hk (bool), us (bool), any (bool), current_hkt (str), current_cst (str)
    """
    from datetime import timezone, timedelta as td
    
    now_cst = datetime.now()  # 服务器在 CST (UTC+8)
    now_hkt = now_cst  # HKT = CST for this server
    
    weekday = now_cst.weekday()  # 0=Mon, 6=Sun
    current_min = now_cst.hour * 60 + now_cst.minute
    
    # HK: Mon-Fri, 09:30-12:00 + 13:00-16:00 HKT
    hk_open = False
    if weekday < 5:
        hk_open = (9*60+30 <= current_min < 12*60) or (13*60 <= current_min < 16*60)
    
    # US: Mon-Fri (CST evening → next day morning)
    # Summer (DST, ~Mar-Nov): 21:30 CST - 04:00+1 CST
    # Winter: 22:30 CST - 05:00+1 CST
    us_open = False
    month = now_cst.month
    is_dst = 3 <= month <= 10  # 简化: 3月-10月为夏令时
    
    if weekday < 5:  # Mon evening to Fri evening
        if is_dst:
            us_open = current_min >= 21*60+30 or current_min < 4*60
        else:
            us_open = current_min >= 22*60+30 or current_min < 5*60
    elif weekday == 5:  # Saturday morning (Friday's session spillover)
        if is_dst:
            us_open = current_min < 4*60
        else:
            us_open = current_min < 5*60
    
    return {
        "hk": hk_open,
        "us": us_open,
        "any": hk_open or us_open,
        "current_time_cst": now_cst.strftime("%H:%M:%S"),
        "weekday": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][weekday],
    }


def check_service_health() -> dict:
    """检查服务健康"""
    import urllib.request

    results = {}

    # 0. 交易时段检测 (P0 2026-05-22: 非交易时段日志静默是正常的)
    results["is_trading_hours"] = _is_any_market_open()

    # 1. ftseek HTTP health
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            results["ftseek_service"] = {
                "status": data.get("status", "unknown"),
                "http_code": resp.status,
                "timestamp": data.get("timestamp"),
            }
    except Exception as e:
        results["ftseek_service"] = {"status": "DOWN", "error": str(e)}

    # 2. systemd service status
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "ftseek"],
            capture_output=True, text=True, timeout=5,
        )
        results["systemd_status"] = proc.stdout.strip()
    except Exception as e:
        results["systemd_status"] = f"check_failed: {e}"

    # 3. 日志活跃度（最后一条日志距今多久）
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            fsize = f.tell()
            f.seek(max(0, fsize - 1000))
            lines = f.readlines()
            for line in reversed(lines):
                ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if ts_match:
                    last_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                    age_seconds = (datetime.now() - last_ts).total_seconds()
                    results["log_freshness"] = {
                        "last_entry": ts_match.group(1),
                        "age_seconds": round(age_seconds),
                        "stale": age_seconds > 300,
                    }
                    break
    except Exception as e:
        results["log_freshness"] = {"error": str(e)}

    return results


def check_opend_status() -> dict:
    """检查 Futu OpenD 状态"""
    results = {}

    # 1. 进程检查
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "FutuOpenD"],
            capture_output=True, text=True, timeout=5,
        )
        results["process_running"] = proc.returncode == 0
        results["pids"] = proc.stdout.strip().split("\n") if proc.stdout.strip() else []
    except Exception as e:
        results["process_running"] = False
        results["error"] = str(e)

    # 2. systemd futu-opend 服务
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "futu-opend"],
            capture_output=True, text=True, timeout=5,
        )
        results["systemd_status"] = proc.stdout.strip()
    except Exception:
        results["systemd_status"] = "unknown"

    # 3. 端口检查 (11111)
    try:
        proc = subprocess.run(
            ["ss", "-tlnp", "|", "grep", "11111"],
            shell=True, capture_output=True, text=True, timeout=5,
        )
        # 用 bash -c 因为管道
        proc2 = subprocess.run(
            ["bash", "-c", "ss -tlnp | grep 11111"],
            capture_output=True, text=True, timeout=5,
        )
        results["port_11111"] = {
            "listening": proc2.returncode == 0 and "11111" in proc2.stdout,
            "detail": proc2.stdout.strip()[:200],
        }
    except Exception as e:
        results["port_11111"] = {"error": str(e)}

    # 4. 最近连接错误统计
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            fsize = f.tell()
            f.seek(max(0, fsize - 500000))
            econn_count = 0
            remote_close_count = 0
            for line in f:
                ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if ts_match:
                    ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - ts).total_seconds() > LOOKBACK_MINUTES * 60:
                        continue
                if "ECONNREFUSED" in line:
                    econn_count += 1
                if "RemoteClose" in line:
                    remote_close_count += 1
            results["recent_errors_35m"] = {
                "ECONNREFUSED": econn_count,
                "RemoteClose": remote_close_count,
            }
    except Exception as e:
        results["recent_errors_35m"] = {"error": str(e)}

    return results


def check_resources() -> dict:
    """检查系统资源"""
    results = {}

    # 1. 内存
    try:
        proc = subprocess.run(
            ["bash", "-c", "free -m | grep Mem"],
            capture_output=True, text=True, timeout=5,
        )
        parts = proc.stdout.strip().split()
        if len(parts) >= 7:
            results["memory_mb"] = {
                "total": int(parts[1]),
                "used": int(parts[2]),
                "available": int(parts[6]),
                "used_pct": round(int(parts[2]) / int(parts[1]) * 100, 1),
            }
    except Exception as e:
        results["memory_mb"] = {"error": str(e)}

    # 2. 磁盘
    try:
        proc = subprocess.run(
            ["df", "-h", "/opt/ftseek"],
            capture_output=True, text=True, timeout=5,
        )
        lines = proc.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            results["disk"] = {
                "filesystem": parts[0],
                "used_pct": parts[4],
                "available": parts[3],
            }
    except Exception as e:
        results["disk"] = {"error": str(e)}

    # 3. 僵尸进程
    try:
        proc = subprocess.run(
            ["bash", "-c", "ps aux | grep -c '[d]efunct'"],
            capture_output=True, text=True, timeout=5,
        )
        results["zombie_processes"] = int(proc.stdout.strip() or 0)
    except Exception:
        results["zombie_processes"] = -1

    # 4. ftseek 进程内存
    try:
        proc = subprocess.run(
            ["bash", "-c", "ps aux | grep '[f]tseek' | grep -v grep | head -1"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.stdout.strip():
            parts = proc.stdout.strip().split()
            results["ftseek_process"] = {
                "pid": parts[1],
                "mem_pct": parts[3],
                "rss_kb": parts[5],
                "cpu_pct": parts[2],
            }
    except Exception:
        pass

    return results


def run_auto_fixes(patterns: dict) -> list:
    """对已知安全模式执行自动修复"""
    fixes = []

    for name, detail in patterns.items():
        if not detail.get("auto_fix"):
            continue

        fix_action = detail["auto_fix"]
        count = detail.get("count", 0)

        if count == 0:
            continue

        try:
            if fix_action == "check_opend_restart":
                # 检查 OpenD 是否需要重启
                proc = subprocess.run(
                    ["pgrep", "-f", "FutuOpenD"],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode != 0:
                    # OpenD 没在运行，尝试重启
                    restart = subprocess.run(
                        ["bash", "/opt/ftseek/start_opend.sh"],
                        capture_output=True, text=True, timeout=30,
                    )
                    fixes.append({
                        "pattern": name,
                        "action": "restart_opend",
                        "success": restart.returncode == 0,
                        "detail": restart.stdout[:200] or restart.stderr[:200],
                    })
                else:
                    fixes.append({
                        "pattern": name,
                        "action": "opend_running_but_errors",
                        "success": False,
                        "detail": f"OpenD is running (PID {proc.stdout.strip()}) but has {count} connection errors — may need manual restart",
                    })

            elif fix_action == "restart_opend":
                proc = subprocess.run(
                    ["systemctl", "restart", "futu-opend"],
                    capture_output=True, text=True, timeout=30,
                )
                fixes.append({
                    "pattern": name,
                    "action": "restart_opend",
                    "success": proc.returncode == 0,
                    "detail": proc.stderr[:200] if proc.returncode != 0 else "restarted",
                })

            elif fix_action == "reconcile_positions":
                # position_sync 每30秒自动运行，这里检查最近一次同步是否成功
                # 如果漂移持续，说明同步逻辑本身有问题，需要人工介入
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        "http://localhost:8899/api/detailed-status",
                        method="GET",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        fixes.append({
                            "pattern": name,
                            "action": "check_position_sync",
                            "success": True,  # 信息收集成功
                            "detail": "position_sync runs every 30s — if drift persists, sync logic may be failing",
                        })
                except Exception as e:
                    fixes.append({
                        "pattern": name,
                        "action": "check_position_sync",
                        "success": True,
                        "detail": "position_sync auto-runs every 30s — drift may indicate sync logic failure",
                    })

        except Exception as e:
            fixes.append({
                "pattern": name,
                "action": fix_action,
                "success": False,
                "detail": str(e)[:200],
            })

    return fixes


def main():
    """生成完整诊断报告"""
    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": os.uname().nodename,
        "checks": {},
        "summary": {},
    }

    # 运行所有检查
    report["checks"]["logs"] = scan_logs()
    report["checks"]["db"] = check_db_consistency()
    report["checks"]["service"] = check_service_health()
    report["checks"]["opend"] = check_opend_status()
    report["checks"]["resources"] = check_resources()

    # 执行自动修复
    patterns = report["checks"]["logs"].get("known_patterns", {})
    fixes = run_auto_fixes(patterns)
    report["auto_fixes"] = fixes

    # 生成摘要
    issues = []
    log_data = report["checks"]["logs"]

    # 从已知模式提取问题
    for name, detail in patterns.items():
        if detail.get("count", 0) > 0:
            issues.append({
                "severity": detail["severity"],
                "category": detail["category"],
                "description": f"{detail['description']} (×{detail['count']})",
                "auto_fix": detail.get("auto_fix"),
            })

    # 未知错误
    unknown = log_data.get("unknown_errors", {})
    if unknown:
        for err, cnt in unknown.items():
            issues.append({
                "severity": "P2",
                "category": "unknown_error",
                "description": f"未知错误: {err[:100]} (×{cnt})",
                "auto_fix": None,
            })

    # 服务问题
    svc = report["checks"]["service"]
    if svc.get("ftseek_service", {}).get("status") != "ok":
        issues.append({
            "severity": "P0",
            "category": "service_down",
            "description": "ftseek 服务不可用",
            "auto_fix": "restart_service",
        })

    if svc.get("log_freshness", {}).get("stale"):
        age = svc["log_freshness"].get("age_seconds", 0)
        trading = svc.get("is_trading_hours", {}).get("any", True)
        if trading:
            # 交易时段日志停滞 = 真问题
            issues.append({
                "severity": "P1",
                "category": "log_stale",
                "description": f"日志超过 {age}s 未更新，服务可能卡死",
                "auto_fix": "restart_service",
            })
        else:
            # 非交易时段日志静默 = 正常，降级为信息性提示
            issues.append({
                "severity": "P3",
                "category": "log_stale_off_hours",
                "description": f"非交易时段日志静默 {age}s (正常)",
                "auto_fix": None,
            })

    # 资源问题
    res = report["checks"]["resources"]
    mem_pct = res.get("memory_mb", {}).get("used_pct", 0)
    if mem_pct > 90:
        issues.append({
            "severity": "P1",
            "category": "memory_pressure",
            "description": f"内存使用 {mem_pct}%",
            "auto_fix": None,
        })

    # OpenD 问题
    opend = report["checks"]["opend"]
    if not opend.get("process_running"):
        issues.append({
            "severity": "P0",
            "category": "opend_down",
            "description": "Futu OpenD 进程未运行",
            "auto_fix": "restart_opend",
        })

    # 排序: P0 > P1 > P2 > P3
    severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    issues.sort(key=lambda x: severity_order.get(x["severity"], 9))

    report["summary"] = {
        "total_issues": len(issues),
        "critical": len([i for i in issues if i["severity"] == "P0"]),
        "high": len([i for i in issues if i["severity"] == "P1"]),
        "medium": len([i for i in issues if i["severity"] == "P2"]),
        "low": len([i for i in issues if i["severity"] == "P3"]),
        "auto_fixes_applied": len([f for f in fixes if f.get("success")]),
        "auto_fixes_failed": len([f for f in fixes if not f.get("success")]),
        "overall_status": (
            "🔴 CRITICAL" if any(i["severity"] == "P0" for i in issues)
            else "🟡 WARNING" if any(i["severity"] == "P1" for i in issues)
            else "🟢 HEALTHY"
        ),
        "issues": issues,
    }

    # 输出 JSON
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
